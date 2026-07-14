#!/bin/bash
# Deploy the current checked-out revision to the judge host and restart its RQ worker.
set -euo pipefail

JUDGE_HOST="${JUDGE_HOST:-64.90.3.112}"
JUDGE_USER="${JUDGE_USER:-root}"
JUDGE_PATH="${JUDGE_PATH:-/root/guwu-oj}"
SOURCE_DIR="$(cd "$(dirname "$0")/.." && pwd)"

printf 'Deploying %s to %s@%s:%s\n' "$SOURCE_DIR" "$JUDGE_USER" "$JUDGE_HOST" "$JUDGE_PATH"

rsync -avz --delete \
  --exclude '.git' \
  --exclude 'venv' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude 'db.sqlite3' \
  --exclude 'staticfiles' \
  --exclude '.env' \
  "$SOURCE_DIR/" "$JUDGE_USER@$JUDGE_HOST:$JUDGE_PATH/"

scp "$SOURCE_DIR/deploy/systemd/guwu-oj-judge-worker.service" \
  "$JUDGE_USER@$JUDGE_HOST:/etc/systemd/system/guwu-oj-judge-worker.service"

ssh "$JUDGE_USER@$JUDGE_HOST" "JUDGE_PATH='$JUDGE_PATH' bash -s" <<'REMOTE'
set -euo pipefail
cd "$JUDGE_PATH"
if [[ ! -d venv ]]; then
  python3 -m venv venv
fi
venv/bin/pip install -r requirements.txt -q
systemctl daemon-reload
systemctl enable guwu-oj-judge-worker
systemctl restart guwu-oj-judge-worker
systemctl status guwu-oj-judge-worker --no-pager
REMOTE
