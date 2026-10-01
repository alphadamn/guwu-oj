"""Atomic claim, lease heartbeat and fencing writeback for judge jobs.

Every judge job executes under a claim regardless of which broker lane it
arrived on. The state machine lives on ``Submission``:

    PENDING ──enqueue──▶ QUEUED ──claim wins──▶ JUDGING ──verdict──▶ DONE
                            ▲                       │
                            └──── reaper requeue ───┘ (heartbeat stale)
                                                    └─ retries exhausted → FAILED

Concurrency safety relies on single-statement, row-locked PostgreSQL
UPDATEs (``UPDATE ... WHERE ... RETURNING``):

* ``claim_submission``      — at most one competing worker wins.
* ``heartbeat_claim``       — lease renewal only while the token still owns
                              the JUDGING row.
* ``finalize_claim``        — verdict writeback is fenced by (token, state);
                              a reaped/stale worker affects zero rows and its
                              side effects (points, solved M2M, notifications)
                              never fire.
* ``requeue_owned_claim``   — worker-side infra retry, fenced by the token.
* ``requeue_stale_claim``   — reaper-only, fenced by the heartbeat cutoff
                              *inside* the UPDATE so a worker that recovers
                              between the reaper's scan and its write keeps
                              the claim.

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


def _valid_verdicts() -> frozenset:
    """Allowed ``Submission.status`` verdict values (apps must be ready)."""
    from .models import Submission

    return frozenset(dict(Submission.STATUS_CHOICES))


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

    ``verdict`` is validated against the model's status choices: the value
    arrives from worker envelopes (a trust boundary), and an unknown verdict
    must fail loudly here rather than being persisted and later rendered.
    """
    if verdict not in _valid_verdicts():
        raise ValueError(f'unknown verdict {verdict!r}')

    now = _now()
    new_state = 'FAILED' if failed else 'DONE'
    sql = """
        UPDATE submissions_submission
           SET status = %s,
               judge_state = %s,
               finished_at = %s,
               result_written_at = %s,
               runtime = %s,
               memory = %s
         WHERE id = %s
           AND claim_token = %s
           AND judge_state = 'JUDGING'
    """
    with connection.cursor() as cur:
        cur.execute(sql, [
            verdict, new_state, now, now, runtime, memory,
            submission_id, token,
        ])
        return cur.rowcount == 1


_CLEAR_CLAIM_SET = """
       judge_state = 'QUEUED',
       worker_id = '',
       claim_token = NULL,
       claimed_at = NULL,
       heartbeat_at = NULL
"""


def requeue_owned_claim(submission_id, token) -> bool:
    """Release the claim **this token owns** back to QUEUED (infra retry).

    Fenced by ``(id, claim_token, state)``: a worker that has already lost
    the lease (reaper requeued it, another worker now owns the row) affects
    zero rows and cannot revoke the new owner. Caller re-enqueues the broker
    job only when this returns ``True``.
    """
    sql = f"""
        UPDATE submissions_submission
           SET {_CLEAR_CLAIM_SET}
         WHERE id = %s
           AND claim_token = %s
           AND judge_state = 'JUDGING'
    """
    with connection.cursor() as cur:
        cur.execute(sql, [submission_id, token])
        return cur.rowcount == 1


def requeue_stale_claim(submission_id, heartbeat_cutoff) -> bool:
    """Reaper-only: clear a dead claim iff the lease is **still** stale.

    The heartbeat predicate is part of the atomic UPDATE rather than a
    separate SELECT, so a worker that heartbeats (or finalises) between the
    reaper's scan and this write keeps its claim. Returns ``True`` only when
    this call actually requeued the row.
    """
    sql = f"""
        UPDATE submissions_submission
           SET {_CLEAR_CLAIM_SET}
         WHERE id = %s
           AND judge_state = 'JUDGING'
           AND heartbeat_at < %s
        RETURNING id
    """
    with connection.cursor() as cur:
        cur.execute(sql, [submission_id, heartbeat_cutoff])
        return cur.fetchone() is not None


def mark_queued(submission_id) -> bool:
    """Move PENDING -> QUEUED at dispatch time.

    Terminal rows are never re-dispatched: duplicate deliveries of an old
    broker message hit this and the caller ACKs them as a no-op.

    ``enqueued_at`` is stamped on every dispatch (including reaper/infra
    requeues) so queue wait is always measured against the attempt that
    actually got claimed.
    """
    sql = """
        UPDATE submissions_submission
           SET judge_state = 'QUEUED',
               enqueued_at = %s
         WHERE id = %s
           AND judge_state IN ('PENDING', 'QUEUED')
    """
    with connection.cursor() as cur:
        cur.execute(sql, [_now(), submission_id])
        return cur.rowcount == 1


# Lifecycle timing columns a worker may stamp while it owns the claim.
_PROGRESS_COLUMNS = frozenset({
    'judge_started_at',
    'container_acquired_at',
    'compile_done_at',
    'tests_done_at',
})


def stamp_progress(submission_id, token, **fields) -> bool:
    """Stamp lifecycle timing columns on the row ``token`` still owns.

    Observability only — the columns feed bottleneck analysis and never
    gate judging, so losing the fence is not an error: it just affects
    zero rows and returns ``False``. A reaped or superseded worker's late
    stamps therefore cannot overwrite the owner's timings.

    ``token is None`` is the legacy no-claim path (direct
    ``judge_submission`` calls from tests/manual rejudge, never the worker
    pipeline) and uses an unfenced ORM write; it logs a warning so accidental
    production use stays visible. Omitted or ``None`` values default to "now".
    """
    unknown = set(fields) - _PROGRESS_COLUMNS
    if unknown:
        raise ValueError(f'not a lifecycle timing column: {sorted(unknown)}')
    stamped = {
        name: value if value is not None else _now()
        for name, value in fields.items()
    }
    if not stamped:
        raise ValueError('stamp_progress requires at least one timing field')
    if token is None:
        from .models import Submission

        logger.warning(
            'Unfenced progress stamp on submission %s (no claim token); '
            'legacy/test path only',
            submission_id,
        )
        return Submission.objects.filter(pk=submission_id).update(**stamped) == 1

    assignments = ', '.join(f'{name} = %s' for name in stamped)
    sql = f"""
        UPDATE submissions_submission
           SET {assignments}
         WHERE id = %s
           AND claim_token = %s
    """
    with connection.cursor() as cur:
        cur.execute(sql, [*stamped.values(), submission_id, token])
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
      The claim is cleared with an atomic, heartbeat-fenced UPDATE
      (:func:`requeue_stale_claim`): a worker that heartbeats between this
      scan and that write keeps its claim and is not re-enqueued.
    * QUEUED rows whose last dispatch (``enqueued_at``, stamped on every
      dispatch by :func:`mark_queued`) is older than
      ``queued_timeout_secs`` — broker dropped the message before any worker
      claimed. These rows never carry a claim; they are only re-enqueued,
      and the ``mark_queued`` gate inside ``enqueue_judge`` absorbs rows that
      a worker claimed in the meantime.
    """
    from .models import Submission

    now = now or _now()
    judging_cutoff = now - timezone.timedelta(seconds=judging_timeout_secs)
    queued_cutoff = now - timezone.timedelta(seconds=queued_timeout_secs)

    requeued = []
    judging_ids = list(
        Submission.objects
        .filter(judge_state='JUDGING', heartbeat_at__lt=judging_cutoff)
        .values_list('id', flat=True)[:limit]
    )
    for sid in judging_ids:
        if requeue_stale_claim(sid, judging_cutoff):
            requeued.append(sid)

    queued_ids = list(
        Submission.objects
        .filter(judge_state='QUEUED', enqueued_at__lt=queued_cutoff)
        .exclude(id__in=requeued)
        .values_list('id', flat=True)[:limit]
    )
    # QUEUED rows need no claim clearing; just re-dispatch. The enqueued
    # ids are returned in (requeued-then-redispatched) order.
    requeued.extend(queued_ids)

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


# Exponent is capped before the power is computed: ``attempt`` comes from a
# Redis counter and a pathological value must not become a giant bignum.
BACKOFF_MAX_EXPONENT = 5  # 0.5 * 2**5 = 32s, itself clipped to 10s below


def sleep_backoff(attempt: int):
    """Small capped backoff before an immediate infra retry."""
    exponent = min(max(int(attempt) - 1, 0), BACKOFF_MAX_EXPONENT)
    time.sleep(min(0.5 * (2 ** exponent), 10))
