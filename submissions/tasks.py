import django
import os
import logging
import queue as _queue
import threading
import time

# Setup Django before importing models
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'oj_project.settings')
django.setup()

from rq import get_current_job
from django.core.cache import cache

from submissions.result_queue import push_envelope

logger = logging.getLogger(__name__)

# Timestamp of the last throttled worker-IP report (see _report_worker_ip).
_ip_reported_at = 0.0


class _AsyncPartialPusher:
    """Background LPUSH for partial-result envelopes.

    The test loop must not block on the round trip to the central Redis
    for every test case: a fat problem carries hundreds of cases, and on
    a WAN-linked judge each synchronous LPUSH costs one full RTT
    (~30-60 ms measured). Partials are best-effort, so a daemon thread
    drains a queue while the loop runs ahead. ``flush`` waits for the
    queue to drain so the terminal result envelope is never ordered
    before its partials on the consumer side.
    """

    def __init__(self, redis_conn):
        import redis

        self._q = _queue.Queue()
        # A dedicated client sharing the connection pool: the worker's
        # own client is not safe to touch from another thread.
        self._redis = redis.Redis(connection_pool=redis_conn.connection_pool)
        self._thread = threading.Thread(
            target=self._loop, name='oj-partial-push', daemon=True,
        )
        self._thread.start()

    def push(self, envelope):
        self._q.put(envelope)

    def flush(self, timeout=10):
        deadline = time.monotonic() + timeout
        while self._q.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.02)

    def _loop(self):
        while True:
            envelope = self._q.get()
            try:
                push_envelope(self._redis, envelope)
            except Exception:
                logger.debug('async partial push failed', exc_info=True)
            finally:
                self._q.task_done()


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
        # Phase boundaries only this worker witnesses; the web-side
        # consumer stamps them onto the submission row.
        'timings': outcome.get('timings') or {},
    }


def _report_worker_ip(client, worker_id, force=False):
    """Announce this worker's edge IP so the direct-link firewall opens.

    Workers behind NAT are renumbered by the ISP, which silently breaks the
    direct base URL. Reporting over the CDN base lets the web side rebuild
    its whitelist. Throttled, because the address is stable for hours at a
    time; failures are ignored (the next report, or the forced retry after
    a failed claim, picks the change up).
    """
    global _ip_reported_at

    from django.conf import settings

    now = time.monotonic()
    interval = float(getattr(settings, 'OJ_JUDGE_IP_REPORT_INTERVAL', 600) or 0)
    if not force and _ip_reported_at and now - _ip_reported_at < interval:
        return
    _ip_reported_at = now
    if force:
        # Recovery path: the caller is about to retry a claim that just
        # failed, so the whitelist must be rebuilt before that retry.
        client.report_ip(worker_id)
        return
    # Otherwise stay off the critical path: the claim must not wait for a
    # CDN round trip, which on a WAN-linked judge is tens of milliseconds.
    threading.Thread(
        target=client.report_ip, args=(worker_id,),
        name=f'judge-report-ip-{worker_id}', daemon=True,
    ).start()


def _judge_task_dbless(submission_id, job):
    """DB-less worker path: HTTP claim -> judge core -> result list."""
    from submissions.case_store import CaseStore
    from submissions.claiming import default_worker_id
    from submissions.judge import truncate_text
    from submissions.judge_core import JudgeSpecError, judge_spec
    from submissions.worker_api import (
        ClaimEndpointUnavailable,
        ClaimLostError,
        HttpHeartbeat,
        JudgeApiClient,
    )

    worker_id = default_worker_id()
    client = JudgeApiClient()

    _report_worker_ip(client, worker_id)

    # A missing/invalid token or malformed bundle is a permanent error for
    # this job; transport failures surface as ClaimEndpointUnavailable.
    try:
        bundle = client.claim(submission_id, worker_id)
    except ClaimEndpointUnavailable as exc:
        # Most likely cause: our NAT address changed, so the direct base is
        # black-holed. Re-report through the CDN base, then try once more.
        logger.warning(
            'Claim API unavailable for %s (%r); re-reporting worker IP',
            submission_id, exc,
        )
        _report_worker_ip(client, worker_id, force=True)
        try:
            bundle = client.claim(submission_id, worker_id)
        except ClaimEndpointUnavailable as retry_exc:
            logger.error(
                'Claim API unavailable for %s: %r', submission_id, retry_exc,
            )
            push_envelope(job.connection, {
                'kind': 'infra', 'submission_id': submission_id,
                'claim_token': None, 'worker_id': worker_id,
                'error': f'claim endpoint unavailable: {retry_exc}',
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
        # Keyed on the test data's content fingerprint, so a re-import of the
        # problem (new key) cannot be served the old bytes.
        store = CaseStore(bundle.get('data_key') or '')

        # Partial-result pushes happen on a background thread: the test
        # loop must not pay the central-Redis RTT for every case.
        pusher = _AsyncPartialPusher(job.connection)

        def load_case_batch(offset, limit):
            # Pulled mid-judging: the claim response carries no test data,
            # so the gap between claimed_at and judge_started_at no longer
            # scales with the size of the problem's test data.
            cached = store.get(offset, limit)
            if cached is not None:
                logger.debug(
                    'Test data cache hit for submission %s (offset %s)',
                    submission_id, offset,
                )
                return cached
            rows = client.fetch_cases(submission_id, token, offset, limit)
            store.put(rows)
            return rows

        def report_case(case):
            # Progress only. A dropped partial costs the user a later
            # refresh, never the verdict, so failures are swallowed.
            #
            # The raw stdout of a case can be megabytes (a correct answer to
            # a big test case *is* megabytes of expected output). Shipping
            # that over the WAN to Redis on every single case dominated the
            # test phase, so trim to what the detail page actually shows --
            # same limit the final outcome uses.
            trimmed = dict(case)
            trimmed['actual_output'] = truncate_text(
                case.get('actual_output', '')
            )
            trimmed['error_message'] = truncate_text(
                case.get('error_message', ''), 2000
            )
            # Enqueue for the background pusher; the test loop continues
            # without waiting for the central-Redis round trip.
            pusher.push({
                'kind': 'partial', 'submission_id': submission_id,
                'claim_token': str(token), 'worker_id': worker_id,
                'case': trimmed,
            })

        spec = {
            'submission_id': submission_id,
            'language': bundle['language'],
            'code': bundle['code'],
            'user_id': bundle.get('user_id', 0),
            'time_limit_ms': bundle['time_limit_ms'],
            'memory_limit_mb': bundle['memory_limit_mb'],
            'total_cases': bundle.get('total_cases') or 0,
            'cases': bundle.get('cases') or [],
            # Function-style problems: the claim bundle carries these two
            # fields (see internal_views.claim_view); without them the
            # compile_cpp dispatch falls through to the standard path,
            # which compiles user code as main.cpp standalone and trips
            # over the grader's `#include "header.h"`.
            'problem_type': bundle.get('problem_type') or 'standard',
            'function_files': bundle.get('function_files') or [],
            # Interactive problems: user-process count and I/O protocol
            # the manager expects (see problems.Problem.interactive_config).
            'interactive_config': bundle.get('interactive_config') or {},
        }
        try:
            outcome = judge_spec(
                spec,
                check_alive=heartbeat.ensure_alive,
                global_timeout_sec=bundle.get('subprocess_timeout_sec'),
                case_loader=load_case_batch,
                on_case_done=report_case,
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
            pusher.flush()
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
            pusher.flush()
            push_envelope(job.connection, {
                'kind': 'infra', 'submission_id': submission_id,
                'claim_token': token, 'worker_id': worker_id,
                'error': f'{type(exc).__name__}: {exc}',
            })
            return None

        # Drain any in-flight partials so they reach the consumer before
        # the authoritative terminal envelope.
        pusher.flush()
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
