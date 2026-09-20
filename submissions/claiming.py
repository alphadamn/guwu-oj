"""Atomic claim, lease heartbeat and fencing writeback for judge jobs.

Every judge job executes under a claim regardless of which broker lane it
arrived on. The state machine lives on ``Submission``:

    PENDING ──enqueue──▶ QUEUED ──claim wins──▶ JUDGING ──verdict──▶ DONE
                            ▲                       │
                            └──── reaper requeue ───┘ (heartbeat stale)
                                                    └─ retries exhausted → FAILED

Concurrency safety relies on three single-statement, row-locked PostgreSQL
UPDATEs (``UPDATE ... WHERE ... RETURNING``):

* ``claim_submission``   — at most one competing worker wins.
* ``heartbeat_claim``    — lease renewal only while the token still owns
                           the JUDGING row.
* ``finalize_claim``     — verdict writeback is fenced by (token, state);
                           a reaped/stale worker affects zero rows and its
                           side effects (points, solved M2M, notifications)
                           never fire.

Raw SQL is used deliberately: the UPDATE-then-RETURNING pattern is the
claim itself and cannot be expressed with the ORM without a separate
SELECT ... FOR UPDATE round trip.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid

from django.conf import settings
from django.db import connection
from django.utils import timezone

logger = logging.getLogger(__name__)

# Redis key counting immediate infra-failure retries per submission.
ATTEMPT_KEY_TTL_SEC = 3600
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_HEARTBEAT_SECS = 15


class ClaimLostError(Exception):
    """Raised when a worker discovers its claim token no longer owns the row.

    The job must abort immediately: another worker owns the submission (or
    the reaper requeued it), and any further writes would be fenced off.
    """


def default_worker_id() -> str:
    import socket

    override = getattr(settings, 'OJ_WORKER_ID', '') or ''
    return override or f'{socket.gethostname()}'


def _now():
    return timezone.now()


def claim_submission(submission_id, worker_id):
    """Atomically claim a queued/pending submission for ``worker_id``.

    Returns the new ``claim_token`` (uuid.UUID) for the winner, otherwise
    ``None`` (another worker owns it, or the row is already terminal).
    """
    token = uuid.uuid4()
    now = _now()
    sql = """
        UPDATE submissions_submission
           SET judge_state = %s,
               worker_id = %s,
               claim_token = %s,
               claimed_at = %s,
               heartbeat_at = %s
         WHERE id = %s
           AND judge_state IN ('PENDING', 'QUEUED')
        RETURNING claim_token
    """
    with connection.cursor() as cur:
        cur.execute(sql, [
            'JUDGING', worker_id, token, now, now, submission_id,
        ])
        row = cur.fetchone()
    if row is None:
        return None
    logger.info(
        'Submission %s claimed by worker %s (token %s)',
        submission_id, worker_id, token,
    )
    return token


def heartbeat_claim(submission_id, token) -> bool:
    """Renew the lease. ``False`` means this worker has lost the claim."""
    now = _now()
    sql = """
        UPDATE submissions_submission
           SET heartbeat_at = %s
         WHERE id = %s
           AND claim_token = %s
           AND judge_state = 'JUDGING'
    """
    with connection.cursor() as cur:
        cur.execute(sql, [now, submission_id, token])
        return cur.rowcount == 1


def finalize_claim(submission_id, token, verdict, runtime=None, memory=None,
                   failed=False):
    """Fenced terminal writeback.

    Returns ``True`` only when this token still owns the JUDGING row. On
    ``True`` the caller is the unique winner and may run side effects
    (points, notifications, cache invalidation); on ``False`` it must
    discard the result. Raw UPDATE bypasses ``post_save``, so callers are
    responsible for publishing the realtime change notification.
    """
    now = _now()
    new_state = 'FAILED' if failed else 'DONE'
    sql = """
        UPDATE submissions_submission
           SET status = %s,
               judge_state = %s,
               finished_at = %s,
               runtime = %s,
               memory = %s
         WHERE id = %s
           AND claim_token = %s
           AND judge_state = 'JUDGING'
    """
    with connection.cursor() as cur:
        cur.execute(sql, [
            verdict, new_state, now, runtime, memory,
            submission_id, token,
        ])
        return cur.rowcount == 1


def requeue_claim(submission_id):
    """Release a claim back to QUEUED (used by the reaper and infra retry).

    Caller is responsible for actually re-enqueuing the broker job only
    when this returns ``True``. Returns ``False`` when the row is no longer
    in a reclaimable JUDGING state (e.g. the original worker recovered and
    already finished).
    """
    sql = """
        UPDATE submissions_submission
           SET judge_state = 'QUEUED',
               worker_id = '',
               claim_token = NULL,
               claimed_at = NULL,
               heartbeat_at = NULL
         WHERE id = %s
           AND judge_state = 'JUDGING'
    """
    with connection.cursor() as cur:
        cur.execute(sql, [submission_id])
        return cur.rowcount == 1


def mark_queued(submission_id) -> bool:
    """Move PENDING -> QUEUED at dispatch time.

    Terminal rows are never re-dispatched: duplicate deliveries of an old
    broker message hit this and the caller ACKs them as a no-op.
    """
    sql = """
        UPDATE submissions_submission
           SET judge_state = 'QUEUED'
         WHERE id = %s
           AND judge_state IN ('PENDING', 'QUEUED')
    """
    with connection.cursor() as cur:
        cur.execute(sql, [submission_id])
        return cur.rowcount == 1


class Claim:
    """Owned-claim handle used inside the judge pipeline.

    Besides the token identity it runs the heartbeat thread and exposes
    ``ensure_alive()`` checkpoints the per-case loop calls between test
    cases.
    """

    def __init__(self, submission_id, token, worker_id,
                 interval_secs=None):
        self.submission_id = submission_id
        self.token = token
        self.worker_id = worker_id
        self.interval_secs = (
            interval_secs
            if interval_secs is not None
            else getattr(settings, 'OJ_JUDGE_HEARTBEAT_SECS', DEFAULT_HEARTBEAT_SECS)
        )
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = None

    def start_heartbeat(self):
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f'judge-hb-{self.submission_id}',
            daemon=True,
        )
        self._thread.start()

    def _heartbeat_loop(self):
        from django.db import close_old_connections

        while not self._stop.wait(self.interval_secs):
            try:
                close_old_connections()
                if not heartbeat_claim(self.submission_id, self.token):
                    logger.warning(
                        'Heartbeat lost claim on submission %s (token %s)',
                        self.submission_id, self.token,
                    )
                    self._lost.set()
                    return
            except Exception:
                # A transient DB blip must not kill the heartbeat loop;
                # the next tick retries, and the reaper remains the
                # authoritative lease-expiry backstop.
                logger.exception(
                    'Heartbeat error for submission %s', self.submission_id,
                )
            finally:
                close_old_connections()

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def ensure_alive(self):
        """Raise :class:`ClaimLostError` if the lease is gone."""
        if self._lost.is_set():
            raise ClaimLostError(
                f'claim for submission {self.submission_id} was revoked'
            )

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def reap_stale_claims(judging_timeout_secs=300, queued_timeout_secs=600,
                      limit=200, now=None):
    """Requeue stale claims and lost QUEUED jobs.

    Returns the list of requeued submission ids. Each returned id is
    re-enqueued on the broker by the caller (``enqueue_judge``) so this
    module never imports the queue layer (avoiding an import cycle).

    * JUDGING rows whose last heartbeat is older than
      ``judging_timeout_secs`` — worker crash / kill -9 / network partition.
    * QUEUED rows older than ``queued_timeout_secs`` (measured from
      created_at) — broker dropped the message before any worker claimed.
    """
    from .models import Submission

    now = now or _now()
    judging_cutoff = now - timezone.timedelta(seconds=judging_timeout_secs)
    queued_cutoff = now - timezone.timedelta(seconds=queued_timeout_secs)

    ids = list(
        Submission.objects
        .filter(judge_state='JUDGING', heartbeat_at__lt=judging_cutoff)
        .values_list('id', flat=True)[:limit]
    )
    ids += list(
        Submission.objects
        .filter(judge_state='QUEUED', created_at__lt=queued_cutoff)
        .exclude(id__in=ids)
        .values_list('id', flat=True)[:limit]
    )

    requeued = []
    for sid in ids:
        # JUDGING rows must clear the dead claim; QUEUED rows only need the
        # dispatch itself. requeue_claim is a no-op for the QUEUED rows.
        requeue_claim(sid)
        requeued.append(sid)
    if requeued:
        logger.warning(
            'Reaper requeued %d stale submissions: %s',
            len(requeued), requeued[:20],
        )
    return requeued


def attempts_key(submission_id) -> str:
    return f'oj:judge:attempts:{submission_id}'


def next_attempt(submission_id, redis_client,
                 max_attempts=DEFAULT_MAX_ATTEMPTS) -> int:
    """Increment and return the infra-retry counter for a submission."""
    key = attempts_key(submission_id)
    value = redis_client.incr(key)
    if value == 1:
        redis_client.expire(key, ATTEMPT_KEY_TTL_SEC)
    return int(value)


def clear_attempts(submission_id, redis_client):
    redis_client.delete(attempts_key(submission_id))


def sleep_backoff(attempt: int):
    """Small capped backoff before an immediate infra retry."""
    time.sleep(min(0.5 * (2 ** max(attempt - 1, 0)), 10))
