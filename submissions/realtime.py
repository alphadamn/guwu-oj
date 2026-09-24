"""Real-time submission status push over Redis pub/sub.

Judge workers (RQ, usually on separate judge hosts) publish lightweight
"submission changed" notifications on the Redis instance they share with
their RQ queue. The web-side ASGI WebSocket service subscribes to every
enabled judge Redis (pub/sub is instance-wide, not database-scoped) plus
the local cache Redis (for web-originated saves) and turns notifications
into database snapshots pushed to the watching browsers.

The JSON payload shape is shared verbatim with the long-polling HTTP API
(``submission_status_api``) so the frontend renders both identically and
can fall back from WebSocket to polling without any glue code.
"""

import json
import logging
from urllib.parse import unquote, urlparse

from django.conf import settings

from .judge import JUDGED_LANGUAGES

logger = logging.getLogger(__name__)

# Per-submission pub/sub channel. Kept stable; subscribers join by id.
CHANNEL_PREFIX = 'oj:submission:'

# Close codes used by the ASGI WebSocket endpoint.
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403


def submission_channel(submission_id):
    return f'{CHANNEL_PREFIX}{submission_id}'


def build_submission_status_payload(submission):
    """Serialise a submission (plus prefetched results) the same way for the
    HTTP polling API, the WebSocket snapshot, and the watchdog."""
    test_results = list(submission.test_results.order_by('case_index'))
    passed_count = sum(1 for r in test_results if r.status == 'Accepted')
    # Authoritative aggregate for Communication problems: sum the
    # manager-reported [0,1] case scores. Standard cases have score=None,
    # so ``earned_score`` stays null and the frontend hides the score line.
    scored = [r for r in test_results if getattr(r, 'score', None) is not None]
    earned_score = round(sum(r.score for r in scored), 4) if scored else None
    problem = submission.effective_problem
    total_cases = problem.test_cases.count() if problem else 0
    # Lifecycle authority (Phase 2); the verdict-based heuristic below stays
    # as a defensive fallback for rows that predate the migration.
    judge_state = getattr(submission, 'judge_state', None)
    if judge_state is not None:
        judging = judge_state not in ('DONE', 'FAILED')
    else:
        judging = (
            submission.status == 'Pending'
            and submission.language in JUDGED_LANGUAGES
            and total_cases > 0
        )

    return {
        'status': submission.status,
        'judge_state': judge_state,
        'worker_id': getattr(submission, 'worker_id', '') or '',
        'runtime': str(submission.runtime),
        'memory': submission.memory,
        'passed_count': passed_count,
        'earned_score': earned_score,
        'total_cases': max(total_cases, len(test_results)),
        'done': not judging,
        'test_results': [
            {
                'case_index': r.case_index,
                'status': r.status,
                'score': getattr(r, 'score', None),
                'runtime': str(r.runtime),
            }
            for r in test_results
        ],
    }


def publish_submission_changed(submission_id):
    """Fire-and-forget pub/sub notify from any sync process (RQ worker/web).

    Never raises: real-time push is best-effort. The WebSocket endpoint also
    runs a slow database watchdog, so a missing notification cannot freeze
    the UI.
    """
    try:
        from django_redis import get_redis_connection

        client = get_redis_connection('default')
        client.publish(
            submission_channel(submission_id),
            json.dumps({'submission_id': submission_id}),
        )
    except Exception:
        logger.debug(
            'publish_submission_changed failed for submission %s',
            submission_id,
            exc_info=True,
        )


def _async_client_from_location(location):
    try:
        import redis.asyncio as aioredis
    except ImportError:
        return None

    if not isinstance(location, str) or not location.startswith(('redis://', 'rediss://')):
        return None

    parsed = urlparse(location)
    kwargs = {
        'host': parsed.hostname or '127.0.0.1',
        'port': parsed.port or 6379,
        'db': int((parsed.path or '/0').lstrip('/') or 0),
        'socket_keepalive': True,
    }
    if parsed.password:
        kwargs['password'] = unquote(parsed.password)
    kwargs.update(getattr(settings, 'CACHE_REDIS_DIRECT_CONNECTION_KWARGS', {}) or {})
    return aioredis.Redis(**kwargs)


def async_cache_client():
    """Async client for the web host's default cache Redis, or ``None`` when
    the deployment has no Redis cache (DEMO_MODE)."""
    location = settings.CACHES.get('default', {}).get('LOCATION', '')
    return _async_client_from_location(location)


def async_judge_clients():
    """One shared async Redis client per enabled judge machine.

    Uses the same TLS/credentials config as the RQ queue connection, because
    the worker publishes on the Redis instance co-located with its queue.
    Returns a list of ``(label, client)``; clients are shared across sockets
    by the caller.
    """
    try:
        import redis.asyncio as aioredis
    except ImportError:
        return []

    # On the web role, JudgeLoadBalancer.machines merges admin-side DB
    # overrides (host/enabled/TLS); workers only see settings.JUDGE_MACHINES.
    try:
        from .judge_load_balancer import load_balancer
        machines = load_balancer.machines
    except Exception:
        machines = getattr(settings, 'JUDGE_MACHINES', [])

    clients = []
    seen = set()
    for machine in machines:
        if not machine.get('enabled', True):
            continue
        key = (machine.get('host'), machine.get('port'), machine.get('db'))
        if key in seen:
            continue
        seen.add(key)
        try:
            from oj_project.settings import _rq_machine_connection

            kwargs = _rq_machine_connection(machine)
            kwargs.update({
                'host': machine['host'],
                'port': machine['port'],
                'db': machine['db'],
                'socket_connect_timeout': 5,
            })
            clients.append((f"judge:{machine['name']}", aioredis.Redis(**kwargs)))
        except Exception:
            logger.warning(
                'Could not build async Redis client for judge %s',
                machine.get('name'),
                exc_info=True,
            )
    return clients
