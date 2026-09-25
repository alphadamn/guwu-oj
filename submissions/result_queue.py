"""``judge:result`` list plumbing between DB-less workers and the consumer.

Reliability is the classic reliable-queue pattern over plain Redis lists:

* workers ``LPUSH`` outcome envelopes onto ``judge:result``;
* the consumer ``BRPOPLPUSH``es each message atomically onto
  ``judge:result:processing`` before touching the database, and only
  ``LREM``-acks it after a successful fenced write;
* on consumer startup anything left in ``:processing`` (crash mid-write)
  is moved back to the main queue for redelivery;
* a message that raises while processing (poison / persistent error) is
  moved to ``judge:result:dead`` instead of looping forever.

At-least-once delivery combined with the claim-token fencing makes
duplicates harmless.
"""

from __future__ import annotations

import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def result_queue_name() -> str:
    return getattr(settings, 'OJ_RESULT_QUEUE_NAME', 'judge:result')


def processing_queue_name() -> str:
    return f'{result_queue_name()}:processing'


def dead_letter_queue_name() -> str:
    return f'{result_queue_name()}:dead'


def broker_client():
    """Redis client connected to the central judge broker.

    The broker URL (``settings.CELERY_BROKER_URL``) is the single source of
    truth on both sides: workers push outcome envelopes onto it, the web-side
    consumer pops them. ``redis.Redis.from_url`` understands the ``ssl_*``
    query parameters used by ``rediss://`` broker URLs.
    """
    import redis

    url = getattr(settings, 'CELERY_BROKER_URL', None)
    if not url:
        raise RuntimeError('CELERY_BROKER_URL is not configured')
    return redis.Redis.from_url(url)


def push_envelope(redis_conn, envelope, queue_name=None):
    """LPUSH a JSON envelope (worker side)."""
    name = queue_name or result_queue_name()
    redis_conn.lpush(name, json.dumps(envelope, ensure_ascii=False))


def recover_processing(redis_conn):
    """Move crash-orphaned processing messages back to the main queue."""
    main = result_queue_name()
    processing = processing_queue_name()
    moved = 0
    while True:
        # RPOPLPUSH takes from the tail of :processing and pushes to the
        # head of the main queue (order irrelevant).
        msg = redis_conn.rpoplpush(processing, main)
        if msg is None:
            break
        moved += 1
    if moved:
        logger.warning('Recovered %d in-flight result message(s)', moved)
    return moved


def fetch_for_processing(redis_conn, timeout_secs=5):
    """Block up to ``timeout_secs`` for one message; returns bytes or None."""
    return redis_conn.brpoplpush(
        result_queue_name(), processing_queue_name(), timeout=timeout_secs,
    )


def ack(redis_conn, raw_message):
    """Remove the processed message from the processing queue."""
    redis_conn.lrem(processing_queue_name(), 1, raw_message)


def dead_letter(redis_conn, raw_message, reason):
    payload = None
    try:
        payload = json.loads(raw_message)
    except Exception:
        payload = {'raw': raw_message.decode('utf-8', 'replace')[:2000]}
    payload['_dead_letter_reason'] = str(reason)[:1000]
    redis_conn.lpush(
        dead_letter_queue_name(),
        json.dumps(payload, ensure_ascii=False)[:2_000_000],
    )
    redis_conn.lrem(processing_queue_name(), 1, raw_message)
