import logging

from django_rq import enqueue

from .tasks import judge_submission_task

logger = logging.getLogger(__name__)


def enqueue_judge(submission_id):
    """Enqueue judge task to RQ queue for async execution."""
    try:
        job = enqueue(judge_submission_task, submission_id)
        logger.info('Enqueued judge task for submission %s, job ID: %s', submission_id, job.id)
        return job
    except Exception:
        logger.exception('Failed to enqueue judge task for submission %s', submission_id)
        raise
