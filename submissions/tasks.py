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
        # Update submission status to indicate error
        submission.status = 'Runtime Error'
        submission.save()
        return None

    # Clear relevant caches
    try:
        cache.delete(f'problem_pass_rate_{submission.problem.id}')
        cache.delete('leaderboard_users')
        cache.delete_pattern('problem_list_query_*')
        cache.delete('home_stats')
    except Exception as e:
        logger.warning(f'Error clearing caches: {e}')

    return result_submission.id
