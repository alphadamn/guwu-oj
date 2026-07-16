import django
import os
import logging

# Setup Django before importing models
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'oj_project.settings')
django.setup()

from rq import get_current_job
from django.core.cache import cache

logger = logging.getLogger(__name__)


def judge_submission_task(submission_id):
    """
    Async task to judge a submission.
    This task is executed by RQ worker.
    """
    from submissions.models import Submission
    from submissions.judge import judge_submission

    job = get_current_job()
    
    try:
        submission = Submission.objects.get(id=submission_id)
        logger.info(f'Processing submission {submission_id} for problem {submission.problem.id}')
    except Submission.DoesNotExist:
        logger.error(f'Submission {submission_id} not found')
        return None

    # Update job metadata
    if job:
        job.meta['submission_id'] = submission_id
        job.meta['problem_id'] = submission.problem.id
        job.meta['user_id'] = submission.user.id
        job.save_meta()

    try:
        # Execute the judge (pass submission ID, not object)
        result_submission = judge_submission(submission_id)
        logger.info(f'Submission {submission_id} judged with status: {result_submission.status}')
    except Exception as e:
        logger.exception(f'Error judging submission {submission_id}: {e}')
        # Unexpected worker/judge failures are infrastructure errors, not
        # errors in the contestant's program.
        submission.status = 'System Error'
        submission.save(update_fields=['status'])
        return None
    finally:
        # Release the judge machine regardless of outcome
        try:
            from submissions.judge_load_balancer import load_balancer
            queue_name = job.origin if job else None
            load_balancer.release_machine(submission_id, queue_name=queue_name)
        except Exception as e:
            logger.warning(f'Error releasing machine for submission {submission_id}: {e}')

    # Clear relevant caches
    try:
        cache.delete(f'problem_pass_rate_{submission.problem.id}')
        cache.delete('leaderboard_users')
        # Problem list cache keys are versioned and invalidated by Problem.
        cache.delete('home_stats')
    except Exception as e:
        logger.warning(f'Error clearing caches: {e}')

    return result_submission.id
