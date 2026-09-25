import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .tasks import judge_submission_task

logger = logging.getLogger(__name__)

# Priority tiers. The Celery Redis broker implements message priority as
# separate list buckets (settings.CELERY_BROKER_TRANSPORT_OPTIONS
# ['priority_steps']) which are BRPOP-consumed in ascending order, so the
# LOWEST number drains first: pro (0) > plus (3) > free (6) > ai (9).
PRIORITY_PRO = 'pro'
PRIORITY_PLUS = 'plus'
PRIORITY_DEFAULT = 'default'   # free users
PRIORITY_AI = 'ai'             # AI judge-tool verification runs (lowest)

# Tier -> Celery message priority (0 = consumed first, 9 = last).
_CELERY_PRIORITY = {
    PRIORITY_PRO: 0,
    PRIORITY_PLUS: 3,
    PRIORITY_DEFAULT: 6,
    PRIORITY_AI: 9,
}

# Single logical queue all judge tasks flow through.
JUDGE_QUEUE = 'judge'


def celery_priority(priority: str) -> int:
    """Map a judge priority tier to its Celery message priority."""
    return _CELERY_PRIORITY.get(priority, _CELERY_PRIORITY[PRIORITY_DEFAULT])


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


def _build_task_payload(submission) -> dict:
    """Self-describing judge task payload for the central queue.

    ``test_case_set_id`` encodes where the test data lives:
    ``problem:<id>`` or ``contest_problem:<id>``.
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
    """Enqueue a judge task on the central Celery broker.

    The single ``judge`` queue carries every tier; per-tier urgency rides on
    the Celery message priority (Redis priority buckets). Dispatch happens in
    ``transaction.on_commit`` so a worker can never claim a Submission row
    that is not committed yet (mirrors the old django-rq on_db_commit mode;
    outside a transaction it fires immediately). Terminal rows are never
    re-dispatched: the ``mark_queued`` gate absorbs duplicate calls.
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

    if not getattr(settings, 'CELERY_BROKER_URL', None):
        # DEMO_MODE has no broker; the row stays QUEUED harmlessly.
        logger.warning(
            'No judge broker configured; submission %s left QUEUED',
            submission_id,
        )
        return None

    priority = resolve_submission_priority(submission)

    def _dispatch():
        judge_submission_task.apply_async(
            args=[submission_id],
            queue=JUDGE_QUEUE,
            priority=celery_priority(priority),
        )
        logger.debug(
            'Enqueued judge task for submission %s (priority=%s)',
            submission_id, priority,
        )

    transaction.on_commit(_dispatch)
    return submission_id
