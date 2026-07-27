#!/usr/bin/env bash
# Interactive setup for a Guwu OJ web host or judge (RQ worker) host.
#
#   sudo bash scripts/oj-setup.sh              # interactive, applies changes
#   sudo bash scripts/oj-setup.sh --dry-run    # print every action, change nothing
#   OJ_SETUP_ENV=/root/guwu-oj/.env sudo bash scripts/oj-setup.sh
#
# Collects configuration first, shows a review screen, and only then touches the
# system. Nothing is installed or written before the final confirmation.
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${OJ_SETUP_ENV:-$PROJECT_DIR/.env}"
DRY_RUN=0
LOG_FILE="/tmp/oj-setup-$(date +%Y%m%d-%H%M%S).log"

for arg in "$@"; do
  case "$arg" in
    --dry-run|-n) DRY_RUN=1 ;;
    --help|-h)
      grep '^#' "${BASH_SOURCE[0]}" | head -12 | cut -c3-
      exit 0 ;;
    *) printf 'unknown argument: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

declare -A CFG=()          # values destined for .env
declare -a PLAN=()         # human-readable action list for the review screen
ROLE=""                    # web | judge | both
INSTALL_PG=0 INSTALL_REDIS=0 INSTALL_DOCKER=0
PG_TLS=0 REDIS_TLS=0 REDIS_TLS_ONLY=0 BUILD_JUDGE_IMAGE=0
INSTALL_WEB_UNIT=0 INSTALL_JUDGE_UNIT=0 RUN_MIGRATIONS=0
PG_HBA_CIDR="" REDIS_BIND="" REDIS_TLS_SANS="" PG_TLS_SANS=""
REDIS_TLS_DIR="/etc/redis/tls" PG_TLS_DIR=""
GUNICORN_BIND="127.0.0.1:4449" GUNICORN_WORKERS="3"

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >>"$LOG_FILE"; }
die() { ui_msg "Error" "$1"; exit 1; }
plan() { PLAN+=("$1"); }


# ---------------------------------------------------------------- UI backend --
# whiptail when available and on a real terminal, plain read() otherwise.
if command -v whiptail >/dev/null 2>&1 && [[ -t 0 && -t 1 ]]; then
  UI=whiptail
else
  UI=text
fi
TITLE="Guwu OJ Setup"

ui_msg() { # title body
  if [[ $UI == whiptail ]]; then
    whiptail --title "$TITLE" --msgbox "$1\n\n$2" 20 78
  else
    printf '\n== %s ==\n%s\n' "$1" "$2"
  fi
}

ui_yesno() { # question default(yes|no) -> 0 for yes
  local q="$1" def="${2:-yes}"
  if [[ $UI == whiptail ]]; then
    local extra=()
    [[ $def == no ]] && extra=(--defaultno)
    whiptail --title "$TITLE" "${extra[@]}" --yesno "$q" 14 78
  else
    local hint="Y/n" ans
    [[ $def == no ]] && hint="y/N"
    read -r -p "$q [$hint] " ans
    ans="${ans:-$def}"
    [[ "$ans" =~ ^([Yy]|[Yy]es)$ ]]
  fi
}

ui_input() { # prompt default [--password] -> echoes value
  local prompt="$1" def="${2:-}" secret="${3:-}" val
  if [[ $UI == whiptail ]]; then
    if [[ $secret == --password ]]; then
      val=$(whiptail --title "$TITLE" --passwordbox "$prompt" 12 78 3>&1 1>&2 2>&3) || return 1
    else
      val=$(whiptail --title "$TITLE" --inputbox "$prompt" 12 78 "$def" 3>&1 1>&2 2>&3) || return 1
    fi
  else
    if [[ $secret == --password ]]; then
      read -r -s -p "$prompt: " val; printf '\n'
    else
      read -r -p "$prompt [${def}]: " val
    fi
  fi
  printf '%s' "${val:-$def}"
}

ui_menu() { # prompt "tag|label" ... -> echoes chosen tag
  local prompt="$1"; shift
  local -a items=() tags=()
  local entry tag label
  for entry in "$@"; do
    tag="${entry%%|*}"; label="${entry#*|}"
    tags+=("$tag"); items+=("$tag" "$label")
  done
  if [[ $UI == whiptail ]]; then
    whiptail --title "$TITLE" --notags --menu "$prompt" 20 78 "${#tags[@]}" \
      "${items[@]}" 3>&1 1>&2 2>&3
  else
    printf '\n%s\n' "$prompt" >&2
    local i=1
    for entry in "$@"; do printf '  %d) %s\n' "$i" "${entry#*|}" >&2; ((i++)); done
    local pick
    read -r -p "choice [1]: " pick
    pick="${pick:-1}"
    printf '%s' "${tags[$((pick-1))]}"
  fi
}


# ------------------------------------------------------------- exec helpers --
run() { # run a privileged/system-changing command, honoring --dry-run
  log "run: $*"
  if (( DRY_RUN )); then
    printf 'DRY-RUN  %s\n' "$*"
    return 0
  fi
  "$@" >>"$LOG_FILE" 2>&1
}

run_sh() { # same, for a shell pipeline given as one string
  log "sh: $1"
  if (( DRY_RUN )); then
    printf 'DRY-RUN  sh -c %q\n' "$1"
    return 0
  fi
  bash -c "$1" >>"$LOG_FILE" 2>&1
}

have() { command -v "$1" >/dev/null 2>&1; }

svc_active() { systemctl is-active --quiet "$1" 2>/dev/null; }

gen_password() { # 24 chars, satisfies the RQ_REDIS_PASSWORD policy in settings.py
  local body
  body=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20)
  printf '%s%s%s%s' "$body" "aZ" "7" "!"
}

gen_secret_key() {
  LC_ALL=C tr -dc 'A-Za-z0-9!@#%^&*(-_=+)' </dev/urandom | head -c 64
}

# settings.py rejects an RQ password lacking a letter, digit, or special char.
validate_redis_password() {
  local p="$1"
  (( ${#p} >= 12 )) || { printf 'must be at least 12 characters'; return 1; }
  [[ "$p" =~ [A-Za-z] ]] || { printf 'must contain a letter'; return 1; }
  [[ "$p" =~ [0-9] ]]    || { printf 'must contain a digit'; return 1; }
  [[ "$p" =~ [^A-Za-z0-9] ]] || { printf 'must contain a special character'; return 1; }
  return 0
}

ask_password() { # prompt varname_for_generate_offer -> echoes password
  local prompt="$1" val reason
  while :; do
    if ui_yesno "$prompt\n\nGenerate a strong password automatically?" yes; then
      gen_password
      return 0
    fi
    val=$(ui_input "$prompt" "" --password) || return 1
    if reason=$(validate_redis_password "$val"); then
      printf '%s' "$val"
      return 0
    fi
    ui_msg "Weak password" "That password $reason. Try again."
  done
}

# Upsert KEY=VALUE into the .env file, preserving unrelated lines and comments.
env_set() {
  local key="$1" value="$2" file="$3"
  if (( DRY_RUN )); then
    case "$key" in
      *PASSWORD*|*SECRET*) printf 'DRY-RUN  %s: %s=<hidden>\n' "$file" "$key" ;;
      *) printf 'DRY-RUN  %s: %s=%s\n' "$file" "$key" "$value" ;;
    esac
    return 0
  fi
  touch "$file"; chmod 600 "$file"
  if grep -qE "^${key}=" "$file"; then
    python3 - "$file" "$key" "$value" <<'PY'
import sys
path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, 'r', encoding='utf-8') as fh:
    lines = fh.readlines()
out = []
for line in lines:
    if line.split('=', 1)[0] == key:
        out.append(f'{key}={value}\n')
    else:
        out.append(line)
with open(path, 'w', encoding='utf-8') as fh:
    fh.writelines(out)
PY
  else
    printf '%s=%s\n' "$key" "$value" >>"$file"
  fi
}

# --------------------------------------------------------------- collection --
welcome() {
  ui_msg "Welcome" "This wizard configures a Guwu OJ web server or judge (RQ worker) host.\n\nProject dir: $PROJECT_DIR\nEnv file:    $ENV_FILE\nLog file:    $LOG_FILE\n\nNothing is installed or changed until you review and confirm the plan at the end.$([[ $DRY_RUN == 1 ]] && printf '\n\nDRY-RUN MODE: no changes will actually be made.')"
}

ask_role() {
  ROLE=$(ui_menu "What is this machine?" \
    "web|Web server (Django + PostgreSQL, admin UI, cache Redis)" \
    "judge|Judge worker (RQ worker + Docker sandbox only)" \
    "both|Both (single-box / dev setup)") || die "Cancelled."
}

ask_web_steps() {
  ui_yesno "Install and configure PostgreSQL locally for this web server?" yes && INSTALL_PG=1
  ui_yesno "Install and configure Redis locally (cache + default judge queue)?" yes && INSTALL_REDIS=1
  ui_yesno "Install the systemd unit for Gunicorn (web app)?" yes && INSTALL_WEB_UNIT=1
  if (( INSTALL_PG )); then
    ui_yesno "Require TLS for PostgreSQL connections (recommended if judges connect over the network)?" no && PG_TLS=1
  fi
}

ask_judge_steps() {
  ui_yesno "Install Docker (required for the sandboxed judge containers)?" yes && INSTALL_DOCKER=1
  (( INSTALL_DOCKER )) && ui_yesno "Build the oj-judge sandbox image now (scripts/build-judge-image.sh)?" yes && BUILD_JUDGE_IMAGE=1
  ui_yesno "Install Redis locally for this judge's own RQ queue?" yes && INSTALL_REDIS=1
  ui_yesno "Install the systemd unit for the judge RQ worker?" yes && INSTALL_JUDGE_UNIT=1
}

ask_redis_tls() {
  (( INSTALL_REDIS )) || return 0
  if [[ "$ROLE" == web ]]; then
    ui_yesno "Require TLS + password on the local cache/queue Redis (recommended if a remote judge connects to it)?" no && REDIS_TLS=1
  else
    ui_yesno "Require TLS + password on this judge's local Redis?" no && REDIS_TLS=1
  fi
}

ask_common() {
  CFG[OJ_ROLE]=$([[ "$ROLE" == judge ]] && echo worker || echo web)

  CFG[DJANGO_SECRET_KEY]=$(ui_input "Django SECRET_KEY (leave blank to auto-generate)" "") || die "Cancelled."
  [[ -z "${CFG[DJANGO_SECRET_KEY]}" ]] && CFG[DJANGO_SECRET_KEY]=$(gen_secret_key)

  if [[ "$ROLE" != judge ]]; then
    CFG[DJANGO_DEBUG]=false
    CFG[DJANGO_ALLOWED_HOSTS]=$(ui_input "DJANGO_ALLOWED_HOSTS (comma separated, e.g. guwu.camluni.cn)" "*") || die "Cancelled."
  fi
}

ask_database() {
  [[ "$ROLE" == judge ]] && (( INSTALL_PG == 0 )) || true
  CFG[DB_NAME]=$(ui_input "PostgreSQL database name" "${CFG[DB_NAME]:-ojdb}") || die "Cancelled."
  CFG[DB_USER]=$(ui_input "PostgreSQL user" "${CFG[DB_USER]:-ojuser}") || die "Cancelled."
  if [[ "$ROLE" == web && $INSTALL_PG == 1 ]]; then
    CFG[DB_HOST]=127.0.0.1
    ui_yesno "Generate a random PostgreSQL password for this new role?" yes \
      && CFG[DB_PASSWORD]=$(gen_password) \
      || CFG[DB_PASSWORD]=$(ui_input "PostgreSQL password" "" --password)
  else
    CFG[DB_HOST]=$(ui_input "PostgreSQL host (web/DB server address)" "${CFG[DB_HOST]:-127.0.0.1}") || die "Cancelled."
    CFG[DB_PASSWORD]=$(ui_input "PostgreSQL password" "" --password) || die "Cancelled."
  fi
  CFG[DB_PORT]=$(ui_input "PostgreSQL port" "${CFG[DB_PORT]:-5432}") || die "Cancelled."

  if [[ "$ROLE" != web ]]; then
    # Judge connecting to a remote DB: offer TLS verification, matching docs/judge-machine-tls.md.
    ui_yesno "Require TLS when connecting to PostgreSQL (verify-ca)?" no && PG_TLS=1
    if (( PG_TLS )); then
      CFG[DB_SSLMODE]=verify-ca
      CFG[DB_SSLROOTCERT]=$(ui_input "Path to the PostgreSQL CA certificate on this host" "/etc/guwu-oj/postgres-tls/ca.crt") || die "Cancelled."
    else
      CFG[DB_SSLMODE]=$(ui_input "DB_SSLMODE" "${CFG[DB_SSLMODE]:-prefer}") || die "Cancelled."
    fi
  elif (( PG_TLS )); then
    CFG[DB_SSLMODE]=verify-ca
    PG_TLS_DIR=$(ui_input "Directory to store the PostgreSQL TLS certificate/key" "/etc/guwu-oj/postgres-tls") || die "Cancelled."
    CFG[DB_SSLROOTCERT]="$PG_TLS_DIR/ca.crt"
    PG_TLS_SANS=$(ui_input "Comma-separated hostnames/IPs judges will use to reach this server (cert SAN)" "$(hostname -f 2>/dev/null || hostname)") || die "Cancelled."
    PG_HBA_CIDR=$(ui_input "CIDR allowed to connect over TLS in pg_hba.conf (e.g. a judge's /32, or 0.0.0.0/0 for any)" "0.0.0.0/0") || die "Cancelled."
  else
    CFG[DB_SSLMODE]=prefer
  fi
}

ask_redis() {
  # Cache Redis: always the web host's Redis, even when configuring a judge.
  if [[ "$ROLE" == judge ]]; then
    CFG[CACHE_REDIS_HOST]=$(ui_input "Cache Redis host (must be the WEB host so the worker can clear web cache keys)" "${CFG[DB_HOST]}") || die "Cancelled."
    CFG[CACHE_REDIS_PASSWORD]=$(ui_input "Cache Redis password on the web host" "" --password) || die "Cancelled."
  else
    CFG[CACHE_REDIS_HOST]=127.0.0.1
    if (( INSTALL_REDIS )); then
      CFG[CACHE_REDIS_PASSWORD]=$(ask_password "Password for the local Redis (used for cache and the local queue)") || die "Cancelled."
    else
      CFG[CACHE_REDIS_PASSWORD]=$(ui_input "Existing local Redis password (blank if none)" "" --password) || die "Cancelled."
    fi
  fi
  CFG[CACHE_REDIS_PORT]=$(ui_input "Cache Redis port" "6379") || die "Cancelled."
  CFG[CACHE_REDIS_DB]=1

  if [[ "$ROLE" == judge ]]; then
    ui_yesno "Does the web host's cache Redis require TLS (rediss://)?" no \
      && CFG[CACHE_REDIS_TLS]=true || CFG[CACHE_REDIS_TLS]=false
    if [[ "${CFG[CACHE_REDIS_TLS]}" == true ]]; then
      CFG[CACHE_REDIS_CA_CERT]=$(ui_input "Path to the cache Redis CA certificate on this host" "/etc/guwu-oj/cache-tls/ca.crt") || die "Cancelled."
    fi
  else
    CFG[CACHE_REDIS_TLS]=$( (( REDIS_TLS )) && echo true || echo false )
    (( REDIS_TLS )) && CFG[CACHE_REDIS_CA_CERT]="$REDIS_TLS_DIR/ca.crt"
  fi

  # RQ Redis: the queue endpoint this host owns (judge) or talks to (web).
  if [[ "$ROLE" == web ]]; then
    CFG[RQ_REDIS_HOST]=127.0.0.1
    CFG[JUDGE_1_HOST]=$(ui_input "judge-1 Redis host as seen from the web server (127.0.0.1 for single-box)" "127.0.0.1") || die "Cancelled."
    CFG[JUDGE_1_PORT]=$(ui_input "judge-1 Redis port" "6379") || die "Cancelled."
    CFG[JUDGE_1_REDIS_DB]=0
    if [[ "${CFG[JUDGE_1_HOST]}" == "127.0.0.1" || "${CFG[JUDGE_1_HOST]}" == "localhost" ]]; then
      CFG[RQ_REDIS_PASSWORD]="${CFG[CACHE_REDIS_PASSWORD]}"
      CFG[RQ_REDIS_TLS]="${CFG[CACHE_REDIS_TLS]}"
    else
      CFG[RQ_REDIS_PASSWORD]=$(ui_input "judge-1 Redis password (as configured on the judge host)" "" --password) || die "Cancelled."
      ui_yesno "Does judge-1 Redis require TLS?" yes && CFG[RQ_REDIS_TLS]=true || CFG[RQ_REDIS_TLS]=false
    fi
  else
    # Judge / both: the RQ queue lives on this machine.
    CFG[RQ_REDIS_HOST]=127.0.0.1
    CFG[JUDGE_1_HOST]=127.0.0.1
    CFG[JUDGE_1_PORT]=6379
    CFG[JUDGE_1_REDIS_DB]=0
    if (( INSTALL_REDIS )); then
      CFG[RQ_REDIS_PASSWORD]=$(ask_password "Password for this judge's local RQ Redis") || die "Cancelled."
    else
      CFG[RQ_REDIS_PASSWORD]=$(ui_input "Existing local RQ Redis password" "" --password) || die "Cancelled."
    fi
    CFG[RQ_REDIS_TLS]=$( (( REDIS_TLS )) && echo true || echo false )
    (( REDIS_TLS )) && CFG[RQ_REDIS_CA_CERT]="$REDIS_TLS_DIR/ca.crt"
  fi
  CFG[RQ_REDIS_PORT]=$(ui_input "RQ Redis port on this host" "6379") || die "Cancelled."
  CFG[RQ_REDIS_DB]=0
}

ask_judge_env() {
  CFG[OJ_MULTI_JUDGE_ENABLED]=true
  CFG[OJ_DOCKER_ENABLED]=true
  CFG[OJ_DOCKER_PIDS_LIMIT]=64
  if [[ "$ROLE" == judge ]]; then
    CFG[OJ_JUDGE_QUEUE]=$(ui_input "Queue name this worker consumes (must match its JudgeMachine row on the web host)" "judge-1") || die "Cancelled."
  fi
}

ask_web_service() {
  (( INSTALL_WEB_UNIT )) || return 0
  GUNICORN_BIND=$(ui_input "Gunicorn bind address (nginx upstream)" "$GUNICORN_BIND") || die "Cancelled."
  GUNICORN_WORKERS=$(ui_input "Gunicorn worker count" "$GUNICORN_WORKERS") || die "Cancelled."
}

ask_migrations() {
  [[ "$ROLE" == judge ]] && return 0
  ui_yesno "Run 'manage.py migrate' and 'collectstatic' after writing the configuration?" yes && RUN_MIGRATIONS=1
}

ask_redis_bind() {
  (( INSTALL_REDIS )) || return 0
  if [[ "$ROLE" == both ]]; then
    REDIS_BIND="127.0.0.1"
  else
    REDIS_BIND=$(ui_input "Redis bind addresses (space separated; add this host's LAN IP if a remote peer must connect)" "127.0.0.1") || die "Cancelled."
  fi
  if (( REDIS_TLS )); then
    REDIS_TLS_DIR=$(ui_input "Directory for the Redis TLS material" "$REDIS_TLS_DIR") || die "Cancelled."
    REDIS_TLS_SANS=$(ui_input "Hostnames/IPs peers use to reach this Redis (cert SAN, comma separated)" "$(hostname -f 2>/dev/null || hostname)") || die "Cancelled."
    ui_yesno "Disable the plaintext Redis port entirely (TLS only)?\n\nOnly do this if every client on this host speaks TLS." no && REDIS_TLS_ONLY=1
  fi
}

collect() {
  welcome
  ask_role
  case "$ROLE" in
    web)   ask_web_steps ;;
    judge) ask_judge_steps ;;
    both)  ask_web_steps; ask_judge_steps ;;
  esac
  ask_redis_tls
  ask_redis_bind
  ask_common
  ask_database
  ask_redis
  ask_judge_env
  ask_web_service
  ask_migrations
}

# ----------------------------------------------------------------- review --
build_plan() {
  PLAN=()
  plan "Role: $ROLE   (.env OJ_ROLE=${CFG[OJ_ROLE]})"
  plan "Write configuration to: $ENV_FILE (mode 600)"
  (( INSTALL_PG ))     && plan "Install PostgreSQL; create database '${CFG[DB_NAME]}' and role '${CFG[DB_USER]}'"
  (( PG_TLS )) && [[ "$ROLE" != judge ]] && plan "Enable PostgreSQL TLS (certs in $PG_TLS_DIR, hostssl for $PG_HBA_CIDR)"
  [[ "$ROLE" != web && "${CFG[DB_SSLMODE]:-}" == verify-ca ]] && plan "Connect to PostgreSQL with sslmode=verify-ca (CA: ${CFG[DB_SSLROOTCERT]})"
  (( INSTALL_REDIS ))  && plan "Install Redis; set requirepass and bind to: $REDIS_BIND"
  (( REDIS_TLS ))      && plan "Enable Redis TLS (certs in $REDIS_TLS_DIR)$( (( REDIS_TLS_ONLY )) && printf ', plaintext port disabled')"
  (( INSTALL_DOCKER )) && plan "Install Docker CE for the judge sandbox"
  (( BUILD_JUDGE_IMAGE )) && plan "Build the oj-judge:latest sandbox image"
  [[ "$ROLE" != web ]] && plan "Load the oj-judge AppArmor profile"
  (( INSTALL_WEB_UNIT ))   && plan "Install + enable Gunicorn systemd unit (bind $GUNICORN_BIND, $GUNICORN_WORKERS workers)"
  (( INSTALL_JUDGE_UNIT )) && plan "Install + enable guwu-oj-judge-worker systemd unit"
  (( RUN_MIGRATIONS )) && plan "Run manage.py migrate + collectstatic"
}

review() {
  build_plan
  local body="" line
  for line in "${PLAN[@]}"; do body+="  • $line"$'\n'; done
  body+=$'\n'"Log file: $LOG_FILE"
  (( DRY_RUN )) && body+=$'\n'"DRY-RUN: nothing will actually be changed."
  if ui_yesno "The following actions will be performed:"$'\n\n'"$body"$'\n'"Proceed?" no; then
    return 0
  fi
  ui_msg "Cancelled" "No changes were made."
  exit 0
}

# ------------------------------------------------------------------ apply --
write_env() {
  log "writing env keys to $ENV_FILE"
  local key
  for key in "${!CFG[@]}"; do
    env_set "$key" "${CFG[$key]}" "$ENV_FILE"
  done
  (( DRY_RUN )) || chmod 600 "$ENV_FILE"
}

apt_install() {
  run env DEBIAN_FRONTEND=noninteractive apt-get update
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
}

apply_postgres() {
  (( INSTALL_PG )) || return 0
  have psql || apt_install postgresql postgresql-contrib
  run systemctl enable --now postgresql
  # Create role + database idempotently. Password is passed via psql variable to
  # avoid quoting issues; never echoed to the log.
  local sql
  sql=$(cat <<PSQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${CFG[DB_USER]}') THEN
    CREATE ROLE "${CFG[DB_USER]}" LOGIN PASSWORD '${CFG[DB_PASSWORD]}';
  ELSE
    ALTER ROLE "${CFG[DB_USER]}" LOGIN PASSWORD '${CFG[DB_PASSWORD]}';
  END IF;
END \$\$;
PSQL
)
  if (( DRY_RUN )); then
    printf 'DRY-RUN  psql: create/alter role %s and database %s\n' "${CFG[DB_USER]}" "${CFG[DB_NAME]}"
  else
    printf '%s\n' "$sql" | sudo -u postgres psql -v ON_ERROR_STOP=1 >>"$LOG_FILE" 2>&1 || die "Failed to create PostgreSQL role."
    sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='${CFG[DB_NAME]}'" | grep -q 1 \
      || sudo -u postgres createdb -O "${CFG[DB_USER]}" "${CFG[DB_NAME]}" >>"$LOG_FILE" 2>&1 \
      || die "Failed to create database."
  fi
  [[ "$ROLE" != judge ]] && (( PG_TLS )) && apply_postgres_tls
}

pg_conf_dir() { # echoes the active PostgreSQL config directory
  local d
  d=$(sudo -u postgres psql -tA -c 'SHOW config_file' 2>/dev/null) && dirname "$d" && return 0
  ls -d /etc/postgresql/*/main 2>/dev/null | head -1
}

apply_postgres_tls() {
  local dir="$PG_TLS_DIR" conf san_args=() s
  run mkdir -p "$dir"
  IFS=',' read -ra sans <<<"$PG_TLS_SANS"
  for s in "${sans[@]}"; do s="${s// /}"; [[ -n "$s" ]] && san_args+=("DNS:$s" "IP:$s"); done
  local san_csv; san_csv=$(IFS=,; echo "${san_args[*]}")
  # Local CA + server cert (server cert signed by the CA judges will verify).
  run_sh "openssl req -x509 -newkey rsa:4096 -nodes -days 3650 \
    -keyout '$dir/ca.key' -out '$dir/ca.crt' -subj '/CN=Guwu OJ Postgres CA'"
  run_sh "openssl req -newkey rsa:4096 -nodes -keyout '$dir/server.key' \
    -out '$dir/server.csr' -subj '/CN=${PG_TLS_SANS%%,*}' -addext 'subjectAltName=${san_csv}'"
  run_sh "openssl x509 -req -in '$dir/server.csr' -CA '$dir/ca.crt' -CAkey '$dir/ca.key' \
    -CAcreateserial -days 825 -out '$dir/server.crt' \
    -extfile <(printf 'subjectAltName=%s' '${san_csv}')"
  run chown postgres:postgres "$dir/server.key" "$dir/server.crt"
  run chmod 600 "$dir/server.key"
  conf=$(pg_conf_dir)
  if [[ -n "$conf" && $DRY_RUN -eq 0 ]]; then
    local pg=$conf/postgresql.conf hba=$conf/pg_hba.conf
    cp -a "$pg" "$pg.bak-$(date +%s)"; cp -a "$hba" "$hba.bak-$(date +%s)"
    run_sh "sed -i \"s|^#*ssl *=.*|ssl = on|\" '$pg'"
    run_sh "sed -i \"s|^#*ssl_cert_file.*|ssl_cert_file = '$dir/server.crt'|\" '$pg'"
    run_sh "sed -i \"s|^#*ssl_key_file.*|ssl_key_file = '$dir/server.key'|\" '$pg'"
    run_sh "sed -i \"s|^#*listen_addresses.*|listen_addresses = '*'|\" '$pg'"
    grep -q "hostssl ${CFG[DB_NAME]} ${CFG[DB_USER]} $PG_HBA_CIDR" "$hba" \
      || printf 'hostssl %s %s %s scram-sha-256\n' "${CFG[DB_NAME]}" "${CFG[DB_USER]}" "$PG_HBA_CIDR" >>"$hba"
    run systemctl restart postgresql
  else
    printf 'DRY-RUN  edit %s postgresql.conf/pg_hba.conf for TLS + hostssl %s\n' "$conf" "$PG_HBA_CIDR"
  fi
  ui_msg "PostgreSQL CA" "Copy $dir/ca.crt to each judge host at ${CFG[DB_SSLROOTCERT]:-/etc/guwu-oj/postgres-tls/ca.crt}. Keep server.key private to this host."
}

redis_conf_file() {
  for f in /etc/redis/redis.conf /etc/redis/redis-server.conf; do
    [[ -f "$f" ]] && { echo "$f"; return 0; }
  done
  echo /etc/redis/redis.conf
}

apply_redis() {
  (( INSTALL_REDIS )) || return 0
  have redis-server || apt_install redis-server
  local conf; conf=$(redis_conf_file)
  if (( DRY_RUN )); then
    printf 'DRY-RUN  set requirepass, bind %s in %s\n' "$REDIS_BIND" "$conf"
  else
    cp -a "$conf" "$conf.bak-$(date +%s)"
    run_sh "sed -i \"s|^#* *requirepass .*|requirepass ${CFG[CACHE_REDIS_PASSWORD]}|\" '$conf'"
    grep -q '^requirepass ' "$conf" || printf 'requirepass %s\n' "${CFG[CACHE_REDIS_PASSWORD]}" >>"$conf"
    run_sh "sed -i \"s|^bind .*|bind $REDIS_BIND|\" '$conf'"
    run_sh "sed -i \"s|^protected-mode .*|protected-mode yes|\" '$conf'"
  fi
  (( REDIS_TLS )) && apply_redis_tls "$conf"
  run systemctl enable redis-server
  run systemctl restart redis-server
}

apply_redis_tls() {
  local conf="$1" dir="$REDIS_TLS_DIR" s san_args=()
  run mkdir -p "$dir"
  IFS=',' read -ra sans <<<"$REDIS_TLS_SANS"
  for s in "${sans[@]}"; do s="${s// /}"; [[ -n "$s" ]] && san_args+=("DNS:$s" "IP:$s"); done
  local san_csv; san_csv=$(IFS=,; echo "${san_args[*]}")
  run_sh "openssl req -x509 -newkey rsa:4096 -nodes -days 3650 \
    -keyout '$dir/ca.key' -out '$dir/ca.crt' -subj '/CN=Guwu OJ Redis CA'"
  run_sh "openssl req -newkey rsa:4096 -nodes -keyout '$dir/redis.key' \
    -out '$dir/redis.csr' -subj '/CN=${REDIS_TLS_SANS%%,*}' -addext 'subjectAltName=${san_csv}'"
  run_sh "openssl x509 -req -in '$dir/redis.csr' -CA '$dir/ca.crt' -CAkey '$dir/ca.key' \
    -CAcreateserial -days 825 -out '$dir/redis.crt' \
    -extfile <(printf 'subjectAltName=%s' '${san_csv}')"
  run chown -R redis:redis "$dir"
  run chmod 600 "$dir/redis.key" "$dir/ca.key"
  if (( DRY_RUN )); then
    printf 'DRY-RUN  configure TLS port in %s%s\n' "$conf" "$( (( REDIS_TLS_ONLY )) && printf ' (plaintext disabled)')"
  else
    { printf '\n# Added by oj-setup.sh\n'
      (( REDIS_TLS_ONLY )) && printf 'port 0\n'
      printf 'tls-port 6379\n'
      printf 'tls-cert-file %s/redis.crt\n' "$dir"
      printf 'tls-key-file %s/redis.key\n' "$dir"
      printf 'tls-ca-cert-file %s/ca.crt\n' "$dir"
      printf 'tls-auth-clients no\n'
    } >>"$conf"
  fi
  ui_msg "Redis CA" "Copy $dir/ca.crt to any remote peer that connects to this Redis over TLS."
}

apply_docker() {
  (( INSTALL_DOCKER )) || return 0
  have docker || run_sh "curl -fsSL https://get.docker.com | sh"
  run systemctl enable --now docker
  if (( BUILD_JUDGE_IMAGE )); then
    if [[ -x "$PROJECT_DIR/scripts/build-judge-image.sh" ]]; then
      run bash "$PROJECT_DIR/scripts/build-judge-image.sh"
    else
      run docker build -t oj-judge:latest "$PROJECT_DIR/docker/judge"
    fi
  fi
}

apply_apparmor() {
  [[ "$ROLE" == web ]] && return 0
  local profile="$PROJECT_DIR/docker/judge/apparmor-profile"
  [[ -f "$profile" ]] || { log "no apparmor profile at $profile"; return 0; }
  run install -D -m 644 "$profile" /etc/apparmor.d/oj-judge
  run apparmor_parser -r /etc/apparmor.d/oj-judge
}

apply_web_unit() {
  (( INSTALL_WEB_UNIT )) || return 0
  local unit=/etc/systemd/system/guwu-oj-web.service
  if (( DRY_RUN )); then
    printf 'DRY-RUN  write %s (gunicorn, bind %s)\n' "$unit" "$GUNICORN_BIND"
  else
    cat >"$unit" <<UNIT
[Unit]
Description=Guwu OJ Web (Gunicorn)
After=network.target postgresql.service redis-server.service

[Service]
Type=simple
User=root
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$ENV_FILE
Environment=OJ_ROLE=web
ExecStart=$PROJECT_DIR/venv/bin/gunicorn oj_project.wsgi:application --bind $GUNICORN_BIND --workers $GUNICORN_WORKERS
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  fi
  run systemctl daemon-reload
  run systemctl enable guwu-oj-web
  run systemctl restart guwu-oj-web
}

apply_judge_unit() {
  (( INSTALL_JUDGE_UNIT )) || return 0
  local src="$PROJECT_DIR/deploy/systemd/guwu-oj-judge-worker.service"
  [[ -f "$src" ]] || { log "missing $src"; return 0; }
  run install -m 644 "$src" /etc/systemd/system/guwu-oj-judge-worker.service
  run systemctl daemon-reload
  run systemctl enable guwu-oj-judge-worker
  run systemctl restart guwu-oj-judge-worker
}

apply_migrations() {
  (( RUN_MIGRATIONS )) || return 0
  local py="$PROJECT_DIR/venv/bin/python"
  [[ -x "$py" ]] || py=python3
  run env OJ_ROLE=web "$py" "$PROJECT_DIR/manage.py" migrate --noinput
  run env OJ_ROLE=web "$py" "$PROJECT_DIR/manage.py" collectstatic --noinput
}

apply() {
  log "apply phase begins (role=$ROLE dry_run=$DRY_RUN)"
  write_env
  apply_postgres
  apply_redis
  apply_docker
  apply_apparmor
  apply_web_unit
  apply_judge_unit
  apply_migrations
  log "apply phase complete"
}

summary() {
  local body="Configuration written to $ENV_FILE."$'\n\n'
  body+="Role: $ROLE"$'\n'
  (( INSTALL_PG ))     && body+="• PostgreSQL ready (db ${CFG[DB_NAME]}, user ${CFG[DB_USER]})"$'\n'
  (( INSTALL_REDIS ))  && body+="• Redis running (bind $REDIS_BIND$( (( REDIS_TLS )) && printf ', TLS on'))"$'\n'
  (( INSTALL_JUDGE_UNIT )) && body+="• Judge worker: systemctl status guwu-oj-judge-worker"$'\n'
  (( INSTALL_WEB_UNIT ))   && body+="• Web: systemctl status guwu-oj-web"$'\n'
  body+=$'\n'"Full log: $LOG_FILE"
  (( DRY_RUN )) && body+=$'\n\n'"DRY-RUN: no changes were actually applied."
  ui_msg "Done" "$body"
}

main() {
  if (( ! DRY_RUN )) && [[ $EUID -ne 0 ]]; then
    printf 'This script needs root to install packages and edit system config. Re-run with sudo, or use --dry-run.\n' >&2
    exit 1
  fi
  : >"$LOG_FILE" 2>/dev/null || LOG_FILE=/dev/null
  collect
  review
  apply
  summary
}

main "$@"
