"""Scoped Docker container cleanup for judge sandbox images."""

import json
import logging
import subprocess
from datetime import datetime, timezone

from dateutil import parser

logger = logging.getLogger(__name__)

JUDGE_IMAGE_PREFIXES = ('oj-cpp', 'oj-c', 'oj-python', 'oj-java', 'oj-other', 'oj-judge')

# Kill running judge containers older than this many seconds.
STALE_RUNNING_SEC = 30


def _is_judge_image(image_ref):
    if not image_ref:
        return False
    return any(image_ref.startswith(prefix) for prefix in JUDGE_IMAGE_PREFIXES)


def _container_image(cid):
    try:
        inspect_res = subprocess.run(
            ['docker', 'inspect', cid, '--format', '{{.Config.Image}}'],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if inspect_res.returncode == 0:
            return inspect_res.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return ''


def _running_seconds(cid):
    try:
        inspect_res = subprocess.run(
            ['docker', 'inspect', cid],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if inspect_res.returncode != 0:
            return None
        data = json.loads(inspect_res.stdout)
        started_at_str = data[0]['State']['StartedAt']
        started_at = parser.isoparse(started_at_str.replace('Z', '+00:00'))
        return (datetime.now(timezone.utc) - started_at).total_seconds()
    except (json.JSONDecodeError, KeyError, ValueError, subprocess.TimeoutExpired, OSError):
        return None


def cleanup_stale_judge_containers(min_running_sec=STALE_RUNNING_SEC, kill_running=True):
    """Kill long-running judge containers; optionally remove Created orphans."""
    try:
        list_res = subprocess.run(
            ['docker', 'ps', '-aq'],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for cid in list_res.stdout.strip().splitlines():
            if not cid:
                continue
            image = _container_image(cid)
            if not _is_judge_image(image):
                continue
            try:
                state_res = subprocess.run(
                    ['docker', 'inspect', cid, '--format', '{{.State.Status}}'],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                status = state_res.stdout.strip() if state_res.returncode == 0 else ''
            except (subprocess.TimeoutExpired, OSError):
                continue

            if status == 'created':
                subprocess.run(['docker', 'rm', '-f', cid], capture_output=True, text=True)
                logger.debug('Removed created judge container %s (%s)', cid, image)
                continue

            if kill_running and status == 'running':
                running_seconds = _running_seconds(cid)
                if running_seconds is not None and running_seconds >= min_running_sec:
                    subprocess.run(['docker', 'kill', cid], capture_output=True, text=True)
                    logger.debug('Killed stale judge container %s after %.1fs', cid, running_seconds)
    except (subprocess.TimeoutExpired, OSError):
        logger.exception('Judge container cleanup failed')


def prune_created_orphans():
    """Remove judge containers stuck in Created state."""
    cleanup_stale_judge_containers(min_running_sec=STALE_RUNNING_SEC, kill_running=False)
