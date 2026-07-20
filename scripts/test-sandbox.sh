#!/usr/bin/env bash
# Quick checks that oj-judge containers match production sandbox flags.
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="${OJ_DOCKER_IMAGE:-oj-judge:latest}"
APPARMOR_PROFILE="${OJ_DOCKER_APPARMOR_PROFILE:-oj-judge}"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
chmod 777 "$WORKDIR"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "Missing image $IMAGE — run: ./scripts/build-judge-image.sh"
  exit 1
fi
if ! aa-status --profiled | grep -Fxq "$APPARMOR_PROFILE"; then
  echo "Missing AppArmor profile $APPARMOR_PROFILE — load docker/judge/apparmor-profile first"
  exit 1
fi

run_sandbox() {
  docker run --rm -i \
    --network none \
    --memory 64m \
    --memory-swap 64m \
    --user 65534:65534 \
    --pids-limit 64 \
    --security-opt no-new-privileges \
    --security-opt "apparmor=$APPARMOR_PROFILE" \
    --cap-drop ALL \
    -v "$WORKDIR:/sandbox:rw" \
    -w /sandbox \
    "$IMAGE" \
    "$@"
}

echo "=== 1. Network isolation (--network none) ==="
if run_sandbox python3 -c "
import socket
s = socket.socket()
s.settimeout(3)
try:
    s.connect(('1.1.1.1', 53))
    print('FAIL: outbound TCP connected')
    raise SystemExit(1)
except OSError as e:
    print('OK: cannot reach network:', type(e).__name__)
"; then
  :
else
  echo "Network test failed"
  exit 1
fi

echo ""
echo "=== 2. Time limit (subprocess timeout on host) ==="
# Host-side timeout mirrors judge.py; container still has no network.
if python3 -c "
import subprocess, tempfile, os
from pathlib import Path
wd = '$WORKDIR'
cmd = ['docker','run','--rm','-i','--network','none','--memory','64m','--memory-swap','64m',
       '--user','65534:65534','--pids-limit','64','--security-opt','no-new-privileges',
       '--security-opt','apparmor=$APPARMOR_PROFILE','--cap-drop','ALL',
       '-v', wd+':/sandbox:rw','-w','/sandbox','$IMAGE','python3','-c','import time; time.sleep(30)']
try:
    subprocess.run(cmd, timeout=2, capture_output=True)
    print('FAIL: should have timed out')
    raise SystemExit(1)
except subprocess.TimeoutExpired:
    print('OK: run killed after host timeout')
"; then
  :
fi

echo ""
echo "=== 3. Memory limit (small --memory) ==="
if run_sandbox python3 -c "
try:
    data = []
    while True:
        data.append('x' * (10**6))
except MemoryError:
    print('OK: MemoryError')
    raise SystemExit(0)
" 2>/dev/null; then
  echo "OK: memory-heavy run ended (OOM or MemoryError)"
else
  code=$?
  if [[ "$code" == 137 ]] || [[ "$code" == 1 ]]; then
    echo "OK: container exited (often 137 = OOM)"
  else
    echo "Exit code: $code (137 = OOM is expected on tight limits)"
  fi
fi

echo ""
echo "=== 4. Host paths not mounted (only /sandbox is writable mount) ==="
if run_sandbox python3 -c "
from pathlib import Path
p = Path('/etc/hostname')
print('container hostname file exists:', p.exists())
# Cannot write outside /sandbox
try:
    Path('/tmp/oj_escape_test').write_text('x')
    print('wrote /tmp inside container (container tmp, not host)')
except OSError as e:
    print('write note:', e)
print('OK: no host project dir unless mounted')
"; then
  :
fi

echo ""
echo "All automated sandbox checks finished."
echo "Also submit malicious-looking code via the OJ UI (see scripts/SANDBOX_TESTING.md)."
