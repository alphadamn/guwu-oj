"""Shared judge machine health checks."""

import logging
import shutil
import subprocess
import time

logger = logging.getLogger(__name__)

REQUIRED_JUDGE_IMAGES = [
    'oj-cpp:latest',
    'oj-c:latest',
    'oj-python:latest',
    'oj-java:latest',
    'oj-other:latest',
]

WORKER_HEARTBEAT_TTL_SEC = 90


def check_redis_ping(redis_client):
    try:
        return redis_client.ping()
    except Exception as exc:
        logger.warning('Redis ping failed: %s', exc)
        return False


def check_worker_heartbeat(redis_client, queue_name):
    """Return True if a worker refreshed its heartbeat recently."""
    try:
        raw = redis_client.get(f'judge:worker:{queue_name}')
        if raw is None:
            return False
        last = int(raw)
        return (time.time() - last) <= WORKER_HEARTBEAT_TTL_SEC
    except Exception as exc:
        logger.warning('Worker heartbeat check failed for %s: %s', queue_name, exc)
        return False


# The central broker connection is process-wide; reuse it across the
# per-machine checks in one health endpoint call.
_central_client_cache = None


def _central_broker_client():
    """Redis client for the central judge broker (``None`` if unavailable)."""
    global _central_client_cache
    if _central_client_cache is None:
        try:
            from submissions.result_queue import broker_client

            _central_client_cache = broker_client()
        except Exception as exc:
            logger.warning('Could not resolve central broker connection: %s', exc)
            _central_client_cache = False
    return _central_client_cache or None


def check_central_worker_heartbeat():
    """Fleet-level worker liveness on the central broker.

    Every Celery judge worker consumes the shared ``judge`` queue and writes
    its ``judge:worker:celery:<host>`` epoch heartbeat onto the central
    broker. Heartbeats are therefore fleet-level: one fresh key means the
    worker fleet is alive and draining.
    """
    client = _central_broker_client()
    if client is None:
        return False, 'central broker unavailable'
    try:
        now = time.time()
        for key in client.scan_iter(match='judge:worker:*'):
            raw = client.get(key)
            if raw is None:
                continue
            try:
                age = now - float(raw)
            except (TypeError, ValueError):
                continue
            if age <= WORKER_HEARTBEAT_TTL_SEC:
                return True, 'ok'
        return False, 'no recent worker heartbeat on central broker'
    except Exception as exc:
        logger.warning('Central worker heartbeat check failed: %s', exc)
        return False, str(exc)[:200]


def check_docker_daemon():
    if shutil.which('docker') is None:
        return False, 'docker binary not found'
    try:
        result = subprocess.run(
            ['docker', 'info'],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True, 'ok'
        return False, (result.stderr or result.stdout or 'docker info failed').strip()[:200]
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, str(exc)


def check_judge_images():
    missing = []
    try:
        result = subprocess.run(
            ['docker', 'images', '--format', '{{.Repository}}:{{.Tag}}'],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False, 'could not list docker images'
        available = set(result.stdout.splitlines())
        for image in REQUIRED_JUDGE_IMAGES:
            if image not in available:
                missing.append(image)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, str(exc)

    if missing:
        return False, f'missing images: {", ".join(missing)}'
    return True, 'ok'


def evaluate_machine_health(machine, redis_client, check_local_docker=False):
    """Return dict of check name -> (ok: bool, detail: str)."""
    checks = {}
    redis_ok = check_redis_ping(redis_client)
    checks['redis'] = (redis_ok, 'ok' if redis_ok else 'ping failed')

    # Celery workers heartbeat to the shared central broker rather than to
    # any machine-local Redis, so liveness is fleet-level.
    hb_ok, hb_detail = check_central_worker_heartbeat()
    checks['worker'] = (hb_ok, hb_detail)

    if check_local_docker:
        docker_ok, docker_detail = check_docker_daemon()
        checks['docker'] = (docker_ok, docker_detail)
        if docker_ok:
            images_ok, images_detail = check_judge_images()
            checks['images'] = (images_ok, images_detail)
        else:
            checks['images'] = (False, 'skipped (docker unavailable)')

    return checks
