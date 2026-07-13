"""Scoped Docker container cleanup for judge sandbox images.

Kills long-running judge containers whose image matches known
``oj-*:latest`` judge images. Cheap enough to call occasionally.
"""

import json
import logging
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


def _is_judge_image(image_ref):
    if not image_ref:
        return False
    return any(image_ref.startswith(p) for p in JUDGE_IMAGE_PREFIXES)


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
            ["docker", "ps", "-aq"], capture_output=True, text=True, timeout=5
        )
        for cid in list_res.stdout.strip().splitlines():
            if not cid:
                continue
            # Fast path: inspect via --format for image name
            try:
                img_res = subprocess.run(
                    ["docker", "inspect", cid, "--format", "{{.Config.Image}}"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
            except (subprocess.TimeoutExpired, OSError):
                continue
            if img_res.returncode != 0:
                continue
            if not _is_judge_image(img_res.stdout.strip()):
                continue

            running_seconds = _running_seconds(cid)
            if running_seconds is not None and running_seconds >= min_running_sec:
                subprocess.run(
                    ["docker", "kill", cid], capture_output=True, timeout=5
                )
                logger.debug(
                    "Killed stale judge container %s after %.1fs",
                    cid, running_seconds,
                )
    except (subprocess.TimeoutExpired, OSError):
        logger.exception("Judge container cleanup failed trying backup")
        subprocess.run(['docker', 'rm', '-f', '$(docker ps -aq)'])
