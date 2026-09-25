"""Celery worker lifecycle hooks for judge machines.

Replaces the old RQ ``AutoReconnectWorker`` bootstrap: when a Celery worker
process becomes ready it warms the per-language judge container pool and
starts writing ``judge:worker:*`` heartbeat keys onto the central broker, so
the web-side fleet health check (``judge_health.check_central_worker_heartbeat``)
keeps working unchanged. Shutdown stops the heartbeat and drains the pool.
"""

import logging
import threading
import time

from celery.signals import worker_ready, worker_shutdown

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SEC = 30
HEARTBEAT_TTL_SEC = 90

_hb_stop = threading.Event()


def _heartbeat_key(worker_id):
    return f'judge:worker:celery:{worker_id}'


def _heartbeat_loop():
    from submissions.claiming import default_worker_id
    from submissions.result_queue import broker_client

    worker_id = default_worker_id()
    client = None
    while not _hb_stop.is_set():
        try:
            if client is None:
                client = broker_client()
            client.set(
                _heartbeat_key(worker_id), int(time.time()),
                ex=HEARTBEAT_TTL_SEC,
            )
        except Exception as exc:
            # Broker blip: drop the client so the next tick reconnects.
            logger.warning('Judge worker heartbeat failed: %s', exc)
            client = None
        if _hb_stop.wait(HEARTBEAT_INTERVAL_SEC):
            break


def _on_worker_ready(sender=None, **kwargs):
    from submissions.container_cleanup import start_container_cleanup
    from submissions.container_pool import start_pool

    start_container_cleanup()
    # Warm the per-language judge container pool in the background so the
    # first submissions after (re)start pay no docker-run overhead.
    start_pool()
    _hb_stop.clear()
    threading.Thread(
        target=_heartbeat_loop, name='judge-worker-heartbeat', daemon=True,
    ).start()
    logger.info('Judge worker ready: container pool + heartbeat started')


def _on_worker_shutdown(sender=None, **kwargs):
    from submissions.container_pool import shutdown_pool

    _hb_stop.set()
    shutdown_pool()


def connect_worker_signals():
    worker_ready.connect(_on_worker_ready)
    worker_shutdown.connect(_on_worker_shutdown)
