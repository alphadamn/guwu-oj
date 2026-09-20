import logging

from django_rq import get_queue
from django.utils import timezone
from rq import Queue
from django.conf import settings
from django.core.cache import cache

from .tasks import judge_submission_task
from .judge_load_balancer import load_balancer

logger = logging.getLogger(__name__)

# Priority tiers, ordered highest -> lowest. RQ workers consume queues in the
# order they are listed on the command line, so a worker listening to
# ``judge-1-pro judge-1-plus judge-1 judge-1-ai`` always drains pro before
# touching plus, etc. The free tier reuses the bare machine queue name so the
# existing worker command keeps working with zero reconfiguration.
PRIORITY_PRO = 'pro'
PRIORITY_PLUS = 'plus'
PRIORITY_DEFAULT = 'default'   # free users
PRIORITY_AI = 'ai'             # AI judge-tool verification runs (lowest)

# Multi-judge: {base_queue} + suffix (free keeps the bare name).
_MULTI_SUFFIX = {
    PRIORITY_PRO: '-pro',
    PRIORITY_PLUS: '-plus',
    PRIORITY_DEFAULT: '',
    PRIORITY_AI: '-ai',
}

# Single-node fallback: map priority to an existing / dedicated queue.
_FALLBACK_QUEUE = {
    PRIORITY_PRO: 'high',
    PRIORITY_PLUS: 'default',
    PRIORITY_DEFAULT: 'low',
    PRIORITY_AI: 'ai',
}


def _is_ai_judge_user(user) -> bool:
    """True when ``user`` is the dedicated AI judge service account."""
    from ai_assistant.judge_tool import _bot_username
    return user.username == _bot_username()


def resolve_submission_priority(submission) -> str:
    """Return the judge-queue priority tier for a submission.

    Order (highest -> lowest): Pro > Plus > Free > AI-judge-tool.
    """
    user = submission.user
    if _is_ai_judge_user(user):
        return PRIORITY_AI

    # Resolve the user's active AI subscription plan. A missing or expired
    # subscription resolves to the free tier.
    try:
        from ai_assistant.quota import resolve_plan
        plan, _ = resolve_plan(user)
    except Exception:
        logger.exception('Failed to resolve plan for user %s', user.id)
        plan = 'free'

    if plan == 'pro':
        return PRIORITY_PRO
    if plan == 'plus':
        return PRIORITY_PLUS
    return PRIORITY_DEFAULT


def _priority_queue_name(base_queue: str, priority: str) -> str:
    suffix = _MULTI_SUFFIX.get(priority, '')
    return f'{base_queue}{suffix}'


def _central_queue_name(priority: str) -> str:
    """Central-broker lane name for a priority tier (``judge:queue{suffix}``)."""
    base = getattr(settings, 'OJ_CENTRAL_QUEUE_NAME', 'judge:queue')
    return f'{base}{_MULTI_SUFFIX.get(priority, "")}'


def is_central_queue_name(queue_name) -> bool:
    """True when ``queue_name`` is one of the central broker judge lanes.

    Used by the worker-side task to skip the legacy per-machine busy-slot
    bookkeeping for centrally dispatched jobs. Deliberately independent of
    the ``OJ_CENTRAL_QUEUE`` flag: that flag is a web-side dispatch decision,
    while workers only see the queue a job actually arrived on, and the
    ``judge:queue*`` name space is unambiguous.
    """
    if not isinstance(queue_name, str):
        return False
    base = getattr(settings, 'OJ_CENTRAL_QUEUE_NAME', 'judge:queue')
    return any(
        queue_name == f'{base}{suffix}' for suffix in _MULTI_SUFFIX.values()
    )


def _central_queue(priority: str):
    """Return the django-rq Queue for the central broker lane, or ``None``.

    ``None`` means the central mode is not active (flag off, DEMO_MODE, or
    the lane was never registered in RQ_QUEUES) and the caller should use
    the legacy dispatch path.
    """
    if not getattr(settings, 'OJ_CENTRAL_QUEUE', False):
        return None
    queue_name = _central_queue_name(priority)
    if queue_name not in getattr(settings, 'RQ_QUEUES', {}):
        logger.warning(
            'Central judge queue %s is not registered in RQ_QUEUES; '
            'falling back to legacy dispatch', queue_name,
        )
        return None
    return get_queue(queue_name)


def _build_task_payload(submission) -> dict:
    """Self-describing judge task payload for the central queue.

    Carried on the job ``meta`` so the worker signature stays unchanged and
    old workers tolerate new payloads. ``test_case_set_id`` encodes where
    the test data lives: ``problem:<id>`` or ``contest_problem:<id>``.
    """
    problem = submission.effective_problem
    if submission.contest_problem_id:
        test_case_set_id = f'contest_problem:{submission.contest_problem_id}'
    elif submission.problem_id:
        test_case_set_id = f'problem:{submission.problem_id}'
    else:
        test_case_set_id = None
    return {
        'submission_id': submission.id,
        'language': submission.language,
        'source_code': submission.code,
        'test_case_set_id': test_case_set_id,
        'time_limit': problem.time_limit if problem else None,
        'memory_limit': problem.memory_limit if problem else None,
        'enqueued_at': timezone.now().isoformat(),
    }


def enqueue_judge(submission_id):
    """Enqueue judge task to RQ queue for async execution.

    With ``OJ_CENTRAL_QUEUE`` enabled the task goes to a single central
    broker lane (``judge:queue{suffix}``) that every judge machine competes
    for — adding or retiring a machine needs no Django-side configuration.
    The self-describing task payload rides on the job ``meta``. If the
    central enqueue fails the legacy per-machine dispatch path below runs
    as a fallback, and the flag switches back to it entirely.
    """
    from submissions.models import Submission

    try:
        submission = Submission.objects.select_related('user').get(id=submission_id)
    except Submission.DoesNotExist:
        logger.error('Cannot enqueue judge: submission %s not found', submission_id)
        return None

    # Lifecycle gate: terminal rows (DONE/FAILED/JUDGING by another owner)
    # are never re-dispatched, so duplicate broker messages are absorbed.
    from submissions.claiming import mark_queued
    if not mark_queued(submission_id):
        logger.info(
            'Skip enqueue for submission %s: not in a dispatchable state',
            submission_id,
        )
        return None

    priority = resolve_submission_priority(submission)

    try:
        if getattr(settings, 'OJ_CENTRAL_QUEUE', False):
            try:
                central_queue = _central_queue(priority)
                if central_queue is not None:
                    payload = _build_task_payload(submission)
                    job = central_queue.enqueue(
                        judge_submission_task,
                        submission_id,
                        meta={
                            'payload': payload,
                            'dispatch_mode': 'central',
                            'submission_id': submission_id,
                            'priority': priority,
                        },
                    )
                    logger.debug(
                        'Enqueued judge task for submission %s (priority=%s) to '
                        'central queue %s, job ID: %s',
                        submission_id, priority, central_queue.name,
                        getattr(job, 'id', 'deferred'),
                    )
                    return job
            except Exception:
                logger.exception(
                    'Central judge queue enqueue failed for submission %s; '
                    'falling back to legacy dispatch', submission_id,
                )

        if getattr(settings, 'OJ_MULTI_JUDGE_ENABLED', False):
            machine = load_balancer.reserve_machine(submission_id)

            if machine:
                try:
                    queue_name = _priority_queue_name(machine['queue'], priority)
                    queue = Queue(
                        queue_name, connection=load_balancer._machine_redis(machine)
                    )
                    job = queue.enqueue(
                        judge_submission_task,
                        submission_id,
                        meta={
                            'judge_machine': machine['name'],
                            'submission_id': submission_id,
                            'priority': priority,
                        },
                    )
                    logger.debug(
                        'Enqueued judge task for submission %s (priority=%s) '
                        'to machine %s queue %s, job ID: %s',
                        submission_id, priority, machine['name'], queue_name, job.id,
                    )
                    return job
                except Exception as e:
                    load_balancer.release_machine(submission_id)
                    logger.error(f'Failed to enqueue to machine {machine["name"]}: {e}')
                    cache_key = f'judge_health_{machine["name"]}'
                    cache.set(cache_key, False, 60)
                    logger.warning(
                        'Marked machine %s as unhealthy, falling back to default queue',
                        machine['name'],
                    )
            else:
                logger.warning('No healthy judge machines available, falling back to default queue')

        fallback_queue = _FALLBACK_QUEUE.get(priority, 'default')
        job = get_queue(fallback_queue).enqueue(judge_submission_task, submission_id)
        # django-rq >= 4 returns None when the enqueue is deferred to the
        # database commit (COMMIT_MODE 'on_db_commit') or to the end of the
        # request ('request_finished'). The job is still queued, just later.
        logger.debug(
            'Enqueued judge task for submission %s (priority=%s) to fallback queue %s, job ID: %s',
            submission_id, priority, fallback_queue, getattr(job, 'id', 'deferred'),
        )
        return job
    except Exception:
        logger.exception('Failed to enqueue judge task for submission %s', submission_id)
        raise
