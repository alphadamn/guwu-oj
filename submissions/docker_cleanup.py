"""Scoped Docker container cleanup for judge sandbox images.

Kills long-running judge containers whose image matches known
``oj-*:latest`` judge images. Cheap enough to call occasionally.

Warm containers from ``submissions.container_pool`` are labelled
``oj.judge.role=pool`` and live as long as their worker process. They are
left alone while the owning pid is alive; pool containers whose owner died
(crashed / SIGKILL worker) are treated as orphans and killed immediately.
"""

import json
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

POOL_ROLE_LABEL = "oj.judge.role"
POOL_WORKER_LABEL = "oj.judge.worker"

_INSPECT_FORMAT = (
    "{{.Config.Image}}|"
    "{{index .Config.Labels \"" + POOL_ROLE_LABEL + "\"}}|"
    "{{index .Config.Labels \"" + POOL_WORKER_LABEL + "\"}}"
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


def _running_seconds(cid):
    try:
        inspect_res = subprocess.run(
            ["docker", "inspect", cid],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if inspect_res.returncode != 0:
            return None
        data = json.loads(inspect_res.stdout)
        started_at_str = data[0]["State"]["StartedAt"]
        started_at = parser.isoparse(started_at_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - started_at).total_seconds()
    except (
        json.JSONDecodeError,
        KeyError,
        ValueError,
        subprocess.TimeoutExpired,
        OSError,
    ):
        return None


def cleanup_stale_judge_containers(
    min_running_sec=STALE_RUNNING_SEC, kill_running=True
):
    try:
        list_res = subprocess.run(
            ["docker", "ps", "-q"], capture_output=True, text=True, timeout=5
        )
        for cid in list_res.stdout.strip().splitlines():
            if not cid:
                continue
            # Fast path: image + pool labels in one inspect call.
            try:
                meta_res = subprocess.run(
                    ["docker", "inspect", cid, "--format", _INSPECT_FORMAT],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
            except (subprocess.TimeoutExpired, OSError):
                continue
            if meta_res.returncode != 0:
                continue
            parts = meta_res.stdout.strip().split("|")
            image_ref = parts[0] if parts else ""
            pool_role = parts[1] if len(parts) > 1 else ""
            pool_owner = parts[2] if len(parts) > 2 else ""
            if not _is_judge_image(image_ref):
                continue

            if pool_role == "pool":
                # Healthy pool containers are managed by the pool itself and
                # may idle for hours; only reap orphans of a dead worker.
                owner_pid = None
                if pool_owner.strip().isdigit():
                    owner_pid = int(pool_owner.strip())
                if owner_pid is not None and _pid_alive(owner_pid):
                    continue
                try:
                    subprocess.run(
                        ["docker", "kill", cid], capture_output=True, timeout=5
                    )
                except (subprocess.TimeoutExpired, OSError):
                    # Retried on the next sweep; one stuck container must not
                    # abort scanning the rest of the host.
                    continue
                logger.info(
                    "Killed orphaned pool container %s (owner pid %s gone)",
                    cid, pool_owner,
                )
                continue

            running_seconds = _running_seconds(cid)
            if running_seconds is not None and running_seconds >= min_running_sec:
                try:
                    subprocess.run(
                        ["docker", "kill", cid], capture_output=True, timeout=5
                    )
                except (subprocess.TimeoutExpired, OSError):
                    continue
                logger.debug(
                    "Killed stale judge container %s after %.1fs",
                    cid, running_seconds,
                )
    except (subprocess.TimeoutExpired, OSError):
        logger.exception("Judge container cleanup failed")
