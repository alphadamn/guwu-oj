import django
import os
import logging

# Setup Django before importing models
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'oj_project.settings')
django.setup()

from rq import get_current_job
from django.core.cache import cache

logger = logging.getLogger(__name__)


def _release_legacy_machine(job):
    """Legacy per-machine busy-slot release (no-op for central lanes)."""
    try:
        from submissions.judge_load_balancer import load_balancer
        from submissions.judge_queue import is_central_queue_name

        queue_name = job.origin if job else None
        if queue_name is not None and not is_central_queue_name(queue_name):
            load_balancer.release_machine(
                job.meta.get('submission_id') if job else None,
                queue_name=queue_name,
            )
    except Exception as exc:
        logger.warning('Error releasing judge machine: %s', exc)


def _retry_or_fail(submission_id, claim, exc):
    """Bounded at-least-once redelivery for infra/unexpected failures.

    Thin wrapper over the shared results helper; the result-queue consumer
    (Phase 3) uses the same logic for DB-less workers' failure envelopes.
    """
    from django_redis import get_redis_connection

    from submissions.results import schedule_retry_or_fail

    schedule_retry_or_fail(
        submission_id,
        get_redis_connection('default'),
        claim.token,
        exc,
    )


def judge_submission_task(submission_id):
    """
    Async task to judge a submission (RQ worker).

    Phase 2: every job first performs an atomic claim. A job that loses the
    race (duplicate delivery, or an already-terminal submission) is ACKed as
    a no-op. The winner runs a heartbeat thread while judging; every verdict
    write is fenced by its claim token, so a worker whose lease was revoked
    cannot overwrite the owner's results or trigger side effects twice.

    Phase 3: with OJ_WORKER_DBLESS enabled the worker holds no PostgreSQL
    credentials at all — it claims via HTTP, judges with the pure core, and
    pushes an outcome envelope onto ``judge:result`` for the web consumer.
    """
    from django.conf import settings

    job = get_current_job()

    if getattr(settings, 'OJ_WORKER_DBLESS', False):
        return _judge_task_dbless(submission_id, job)

    return _judge_task_db(submission_id, job)


def _result_envelope(submission_id, token, worker_id, outcome):
    return {
        'kind': 'result',
        'submission_id': submission_id,
        'claim_token': str(token),
        'worker_id': worker_id,
        'verdict': outcome['verdict'],
        'runtime_ms': outcome.get('runtime_ms') or 0,
        'memory_kb': outcome.get('memory_kb') or 0,
        'cases': outcome.get('cases') or [],
    }


def _judge_task_dbless(submission_id, job):
    """DB-less worker path: HTTP claim -> judge core -> result list."""
    from submissions.claiming import default_worker_id
    from submissions.judge_core import JudgeSpecError, judge_spec
    from submissions.result_queue import push_envelope
    from submissions.worker_api import (
        ClaimEndpointUnavailable,
        ClaimLostError,
        HttpHeartbeat,
        JudgeApiClient,
    )

    worker_id = default_worker_id()
    client = JudgeApiClient()

    # A missing/invalid token or malformed bundle is a permanent error for
    # this job; transport failures surface as ClaimEndpointUnavailable.
    try:
        bundle = client.claim(submission_id, worker_id)
    except ClaimEndpointUnavailable as exc:
        logger.error('Claim API unavailable for %s: %r', submission_id, exc)
        push_envelope(job.connection, {
            'kind': 'infra', 'submission_id': submission_id,
            'claim_token': None, 'worker_id': worker_id,
            'error': f'claim endpoint unavailable: {exc}',
        })
        return None

    if not bundle.get('claimable'):
        logger.info(
            'DBless job for submission %s ACKed: not claimable (worker %s)',
            submission_id, worker_id,
        )
        return None

    token = bundle['claim_token']
    heartbeat = HttpHeartbeat(client, submission_id, token)
    heartbeat.start()
    try:
        spec = {
            'submission_id': submission_id,
            'language': bundle['language'],
            'code': bundle['code'],
            'user_id': bundle.get('user_id', 0),
            'time_limit_ms': bundle['time_limit_ms'],
            'memory_limit_mb': bundle['memory_limit_mb'],
            'cases': bundle.get('cases') or [],
        }
        try:
            outcome = judge_spec(
                spec,
                check_alive=heartbeat.ensure_alive,
                global_timeout_sec=bundle.get('subprocess_timeout_sec'),
            )
        except ClaimLostError:
            logger.warning(
                'DBless submission %s lost its lease; outcome discarded',
                submission_id,
            )
            return None
        except (JudgeSpecError,) as exc:
            # Permanent data problem: surface as System Error through the
            # fenced write (consumer turns it into a terminal verdict).
            push_envelope(job.connection, {
                'kind': 'result', 'submission_id': submission_id,
                'claim_token': token, 'worker_id': worker_id,
                'verdict': 'System Error', 'runtime_ms': 0,
                'memory_kb': 0, 'cases': [],
                'error': str(exc),
            })
            return None
        except Exception as exc:
            logger.exception(
                'DBless judging failed for submission %s', submission_id,
            )
            push_envelope(job.connection, {
                'kind': 'infra', 'submission_id': submission_id,
                'claim_token': token, 'worker_id': worker_id,
                'error': f'{type(exc).__name__}: {exc}',
            })
            return None

        push_envelope(
            job.connection,
            _result_envelope(submission_id, token, worker_id, outcome),
        )
        logger.info(
            'DBless submission %s outcome pushed (%s)',
            submission_id, outcome['verdict'],
        )
        return submission_id
    finally:
        heartbeat.stop()


def _judge_task_db(submission_id, job):
    from submissions.claiming import (
        Claim,
        ClaimLostError,
        claim_submission,
        clear_attempts,
        default_worker_id,
    )
    from submissions.judge import judge_submission
    from submissions.realtime import publish_submission_changed

    worker_id = default_worker_id()

    token = claim_submission(submission_id, worker_id)
    if token is None:
        # At-least-once duplicate, or the row is already terminal: ACK and
        # discard without touching the database result.
        logger.info(
            'Job for submission %s ACKed without judging: '
            'not claimable (worker %s)',
            submission_id, worker_id,
        )
        _release_legacy_machine(job)
        return None

    claim = Claim(submission_id, token, worker_id)
    claim.start_heartbeat()
    result_submission = None
    try:
        try:
            result_submission = judge_submission(submission_id, claim=claim)
        except ClaimLostError:
            logger.warning(
                'Submission %s judged by a stale worker context, '
                'discarding result (token %s)',
                submission_id, token,
            )
            return None
        except Exception as exc:
            logger.exception('Error judging submission %s', submission_id)
            _retry_or_fail(submission_id, claim, exc)
            return None
        else:
            try:
                from django_redis import get_redis_connection
                clear_attempts(submission_id, get_redis_connection('default'))
            except Exception:
                logger.debug('could not clear attempts counter', exc_info=True)
            # Fenced raw UPDATEs bypass post_save; push the realtime signal.
            publish_submission_changed(submission_id)
    finally:
        claim.stop()
        _release_legacy_machine(job)

    # Clear relevant caches
    try:
        if result_submission is not None and result_submission.problem_id:
            cache.delete(f'problem_pass_rate_{result_submission.problem_id}')
        cache.delete('leaderboard_users')
        # Problem list cache keys are versioned and invalidated by Problem.
        cache.delete('home_stats')
    except Exception as e:
        logger.warning(f'Error clearing caches: {e}')

    return result_submission.id
