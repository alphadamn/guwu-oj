"""Scoped Docker container cleanup for judge sandbox images.

Reaps three kinds of container while leaving the ones in active use alone:

* Pooled containers (``oj.judge.role=pool``) live as long as their worker
  process. They are left alone while the owning pid is alive and removed
  once it dies (crashed / SIGKILLed worker).
* Containers stuck in the ``created`` state. A ``docker run`` that was
  interrupted (worker timeout, crashed CLI, stalled daemon) leaves the
  container created but never started; nothing ever reaps it because
  Docker's ``--rm`` only fires once the container has actually run. Each
  one pins a writable overlay layer forever and makes every later daemon
  call slower, so they accumulate into a slow-motion outage.
* Plain judge containers running past *min_running_sec* — the one-shot
  fallback path, i.e. a judge that has lost its sandbox.

All of this is derived from a single ``docker ps -a`` call: the listing
already carries image, state, creation time and labels, so there is no
``docker inspect`` per container (one daemon round-trip per sweep instead
of one per container).
"""

import logging
import os
import subprocess
from datetime import datetime, timezone

from dateutil import parser

logger = logging.getLogger(__name__)

JUDGE_IMAGE_PREFIXES = (
    "oj-cpp",
    "oj-c",
    "oj-python",
    "oj-java",
    "oj-other",
    "oj-judge",
)

STALE_RUNNING_SEC = 30

# A never-started container is only reaped once it is clearly abandoned.
# This comfortably exceeds the 30 s ``docker run`` timeout in sandbox.py, so
# a create that is merely slow is never reaped out from under its caller.
STALE_CREATED_SEC = 120

POOL_ROLE_LABEL = "oj.judge.role"
POOL_WORKER_LABEL = "oj.judge.worker"

_PS_FORMAT = (
    "{{.ID}}|{{.Image}}|{{.State}}|{{.CreatedAt}}|"
    "{{index .Labels \"" + POOL_ROLE_LABEL + "\"}}|"
    "{{index .Labels \"" + POOL_WORKER_LABEL + "\"}}"
)


def _is_judge_image(image_ref):
    if not image_ref:
        return False
    return any(image_ref.startswith(p) for p in JUDGE_IMAGE_PREFIXES)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _age_seconds(created_at):
    """Seconds since a ``docker ps`` CreatedAt stamp, or None if unparseable."""
    if not created_at:
        return None
    tokens = created_at.split()
    # "2026-09-17 14:29:53 +0800 CST" — drop the redundant tz abbreviation,
    # which dateutil is not guaranteed to accept next to a numeric offset.
    if tokens and tokens[-1].isalpha():
        tokens.pop()
    try:
        created = parser.parse(" ".join(tokens))
    except (ValueError, OverflowError, TypeError):
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds()


def _remove(cid, reason):
    try:
        subprocess.run(
            ["docker", "rm", "-f", cid], capture_output=True, timeout=10
        )
    except (subprocess.TimeoutExpired, OSError):
        # Retried on the next sweep; one stuck container must not abort
        # scanning the rest of the host.
        return False
    logger.info("Removed %s container %s", reason, cid)
    return True


def _parse_row(line):
    parts = line.split("|")
    if len(parts) < 6:
        return None
    return parts[:6]


def cleanup_stale_judge_containers(
    min_running_sec=STALE_RUNNING_SEC, kill_running=True
):
    try:
        list_res = subprocess.run(
            ["docker", "ps", "-a", "--format", _PS_FORMAT],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        logger.exception("Judge container cleanup failed")
        return
    if list_res.returncode != 0:
        return

    for line in list_res.stdout.splitlines():
        row = _parse_row(line)
        if row is None:
            continue
        cid, image_ref, state, created_at, pool_role, pool_owner = row
        if not _is_judge_image(image_ref):
            continue

        if pool_role == "pool":
            # Healthy pool containers are managed by the pool itself and may
            # idle for hours; only reap orphans of a dead worker.
            owner_pid = None
            if pool_owner.strip().isdigit():
                owner_pid = int(pool_owner.strip())
            if owner_pid is not None and _pid_alive(owner_pid):
                continue
            _remove(cid, "orphaned pool")
            continue

        if state == "created":
            age = _age_seconds(created_at)
            if age is not None and age >= STALE_CREATED_SEC:
                _remove(cid, "leaked never-started")
            continue

        if state != "running" or not kill_running:
            continue

        age = _age_seconds(created_at)
        if age is not None and age >= min_running_sec:
            _remove(cid, "stale judge")
