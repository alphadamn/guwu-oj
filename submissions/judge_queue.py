import logging

from django_rq import get_queue
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


def enqueue_judge(submission_id):
    """Enqueue judge task to RQ queue for async execution with load balancing.

    The target queue is chosen by the submitter's subscription tier so that
    paid plans are judged ahead of free users, and AI-explanation verification
    runs are deprioritised below everything else.
    """
    from submissions.models import Submission

    try:
        submission = Submission.objects.select_related('user').get(id=submission_id)
    except Submission.DoesNotExist:
        logger.error('Cannot enqueue judge: submission %s not found', submission_id)
        return None

    priority = resolve_submission_priority(submission)

    try:
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
