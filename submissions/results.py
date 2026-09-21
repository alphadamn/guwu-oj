"""Web-side persistence of judge outcomes (Phase 3).

The result-queue consumer is the ONLY writer path in the DB-less design:
judge workers never hold PostgreSQL credentials. Everything here is
idempotent and fencing-protected:

* the terminal verdict, the case-result replacement and the Accepted
  side effects happen in ONE transaction gated by the claim token, so a
  crashed consumer redelivers a clean full write instead of a partial one,
  and a stale worker's late envelope affects zero rows;
* rewards are intrinsically idempotent (M2M add + point ledger keyed on
  (user, event_type, event_key));
* infra failures are re-enqueued a bounded number of times (Redis
  counter) before being fenced as FAILED/System Error.
"""

from __future__ import annotations

import logging
from datetime import datetime

from django.db import transaction

from .claiming import (
    DEFAULT_MAX_ATTEMPTS,
    clear_attempts,
    finalize_claim,
    next_attempt,
    requeue_claim,
    sleep_backoff,
    stamp_progress,
)

logger = logging.getLogger(__name__)

# Worker-reported phase boundaries accepted from the result envelope. The
# web side owns every other timestamp column.
_WORKER_TIMING_FIELDS = ('judge_started_at', 'compile_done_at', 'tests_done_at')


def _parse_timings(raw):
    """Parse worker-reported phase timestamps; drop unusable values.

    A DB-less worker never shares the database clock, so these are only as
    trustworthy as its NTP sync. They feed analysis charts, never a
    correctness decision, so malformed input is logged and ignored rather
    than failing an otherwise good verdict.
    """
    if not isinstance(raw, dict):
        return {}
    parsed = {}
    for name in _WORKER_TIMING_FIELDS:
        value = raw.get(name)
        if not value:
            continue
        try:
            parsed[name] = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            logger.warning(
                'Ignoring unparseable %s %r in result envelope', name, value,
            )
    return parsed


def _ordered_case_rows(submission):
    """Return the 1-based-index -> ORM test-case mapping for a submission."""
    if submission.contest_problem_id is not None:
        return list(submission.contest_problem.test_cases.order_by('order', 'id'))
    if submission.problem_id is not None:
        return list(submission.problem.test_cases.order_by('order', 'id'))
    return []


def grant_accepted_rewards(submission, problem):
    """solved M2M + per-test-case points. Idempotent on its own."""
    if submission.contest_problem_id is None:
        submission.user.solved_problems.add(problem)
        from points.models import PointConfig
        from points.services import apply_points

        reward_points = PointConfig.get_solo().accepted_testcase_points
        if reward_points:
            for result in submission.test_results.filter(
                status='Accepted'
            ).select_related('test_case'):
                if result.test_case_id and not result.test_case.is_sample:
                    apply_points(
                        user_id=submission.user_id,
                        amount=reward_points,
                        event_type='accepted_testcase',
                        event_key=f'{problem.id}:{result.test_case_id}',
                        description=f'首次通过 {problem.title} 的测试点 #{result.case_index}',
                    )


def write_judge_outcome(submission_id, token, verdict, runtime_ms,
                        memory_kb, cases, timings=None):
    """Persist a worker outcome behind its claim fence. Returns ``bool``.

    ``False`` means the envelope was stale (claim revoked / row already
    terminal) and was discarded without any side effect. ``timings`` are
    the worker-reported phase boundaries (already parsed to datetimes);
    they are stamped under the same token, so a stale envelope cannot
    write them either.
    """
    from .models import Submission, SubmissionTestResult

    try:
        submission = (
            Submission.objects
            .select_related('problem', 'contest_problem', 'user')
            .get(pk=submission_id)
        )
    except Submission.DoesNotExist:
        logger.error('Result envelope for missing submission %s', submission_id)
        return False

    problem = submission.effective_problem
    case_rows = _ordered_case_rows(submission)

    try:
        with transaction.atomic():
            # Fence first: a stale/duplicate envelope stops here before any
            # row is deleted.
            won = finalize_claim(
                submission_id, token, verdict,
                runtime=runtime_ms, memory=memory_kb, failed=False,
            )
            if not won:
                logger.warning(
                    'Discarding stale result envelope for submission %s '
                    '(token %s)', submission_id, token,
                )
                return False

            if timings:
                stamp_progress(submission_id, token, **timings)

            SubmissionTestResult.objects.filter(submission_id=submission_id).delete()
            for case in cases:
                idx = int(case['index'])
                tc = case_rows[idx - 1] if 0 < idx <= len(case_rows) else None
                relation = (
                    {'contest_test_case': tc}
                    if submission.contest_problem_id is not None
                    else {'test_case': tc}
                )
                SubmissionTestResult.objects.create(
                    submission_id=submission_id,
                    case_index=idx,
                    status=case['status'],
                    runtime=case.get('runtime_ms'),
                    actual_output=(case.get('actual_output') or '')[:4000],
                    expected_output='',
                    error_message=(case.get('error_message') or '')[:2000],
                    **relation,
                )

            if verdict == 'Accepted' and problem is not None:
                # Reload for the reward query inside the same transaction.
                grant_accepted_rewards(submission, problem)
    except Exception:
        logger.exception(
            'Failed to persist outcome for submission %s; envelope will be '
            'redelivered', submission_id,
        )
        raise

    logger.info(
        'Submission %s outcome persisted: %s (%d cases)',
        submission_id, verdict, len(cases),
    )
    return True


def fail_unclaimed(submission_id):
    """Terminal FAILED for a row that never reached JUDGING (claim-phase)."""
    from django.db import connection
    from django.utils import timezone

    sql = """
        UPDATE submissions_submission
           SET status = 'System Error',
               judge_state = 'FAILED',
               finished_at = %s,
               result_written_at = %s,
               runtime = 0
         WHERE id = %s
           AND judge_state = 'QUEUED'
           AND claim_token IS NULL
    """
    with connection.cursor() as cur:
        now = timezone.now()
        cur.execute(sql, [now, now, submission_id])
        return cur.rowcount == 1


def schedule_retry_or_fail(submission_id, redis_client, token, error,
                           enqueue=None, sleep=None):
    """Bound infra retries for both DB and DB-less failure envelopes.

    ``token`` is the live claim token for judging-phase failures (the row
    is JUDGING), or ``None`` when the worker could not even claim (row
    still QUEUED). Returns ``'requeued'`` or ``'failed'``.
    """
    from .judge_queue import enqueue_judge
    enqueue = enqueue or enqueue_judge
    sleep = sleep or sleep_backoff

    attempt = next_attempt(submission_id, redis_client)
    if attempt >= DEFAULT_MAX_ATTEMPTS:
        logger.error(
            'Submission %s exhausted %d infra retries: %r',
            submission_id, attempt, error,
        )
        if token is not None:
            won = finalize_claim(
                submission_id, token, 'System Error',
                runtime=0, failed=True,
            )
        else:
            won = fail_unclaimed(submission_id)
        clear_attempts(submission_id, redis_client)
        if won:
            from .realtime import publish_submission_changed
            publish_submission_changed(submission_id)
        return 'failed'

    logger.warning(
        'Submission %s infra failure (attempt %d/%d), requeueing: %r',
        submission_id, attempt, DEFAULT_MAX_ATTEMPTS, error,
    )
    if token is not None:
        moved = requeue_claim(submission_id)
    else:
        # Claim-phase failure: re-enqueue only if nobody claimed meanwhile.
        from .models import Submission
        moved = Submission.objects.filter(
            id=submission_id, judge_state='QUEUED', claim_token__isnull=True,
        ).exists()
    if moved:
        sleep(attempt)
        enqueue(submission_id)
        return 'requeued'
    return 'discarded'


def process_envelope(raw, redis_conn):
    """Handle one raw result-queue message. Returns ``'ok'`` or raises.

    ``'ok'`` includes deliberately-discarded stale envelopes (they must be
    acked, not retried forever). Any unexpected exception propagates so the
    command loop can dead-letter the message.
    """
    import json
    import uuid

    from .realtime import publish_submission_changed

    env = json.loads(raw)
    submission_id = env.get('submission_id')
    if submission_id is None:
        raise ValueError('envelope missing submission_id')
    token_raw = env.get('claim_token')
    token = uuid.UUID(token_raw) if token_raw else None
    kind = env.get('kind', 'result')

    if kind == 'infra':
        outcome = schedule_retry_or_fail(
            submission_id, redis_conn, token,
            env.get('error', 'infrastructure failure'),
        )
        logger.info('Infra envelope for %s -> %s', submission_id, outcome)
        return 'ok'

    if kind != 'result':
        raise ValueError(f'unknown envelope kind {kind!r}')

    won = write_judge_outcome(
        submission_id,
        token,
        env.get('verdict', 'System Error'),
        int(env.get('runtime_ms') or 0),
        env.get('memory_kb'),
        env.get('cases') or [],
        timings=_parse_timings(env.get('timings')),
    )
    if won:
        publish_submission_changed(submission_id)
    return 'ok'
