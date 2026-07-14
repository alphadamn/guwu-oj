import logging

from django_rq import enqueue, get_queue
from django.conf import settings
from django.core.cache import cache

from .tasks import judge_submission_task
from .judge_load_balancer import load_balancer

logger = logging.getLogger(__name__)


def enqueue_judge(submission_id):
    """Enqueue judge task to RQ queue for async execution with load balancing."""
    try:
        if getattr(settings, 'OJ_MULTI_JUDGE_ENABLED', False):
            machine = load_balancer.select_machine()

            if machine:
                try:
                    queue = get_queue(machine['queue'])
                    job = queue.enqueue(judge_submission_task, submission_id)
                    load_balancer._incr_busy(machine)
                    load_balancer._set_submission_machine(submission_id, machine['name'])
                    job.meta['judge_machine'] = machine['name']
                    job.meta['submission_id'] = submission_id
                    job.save_meta()
                    logger.info(
                        'Enqueued judge task for submission %s to machine %s, job ID: %s',
                        submission_id, machine['name'], job.id,
                    )
                    return job
                except Exception as e:
                    logger.error(f'Failed to enqueue to machine {machine["name"]}: {e}')
                    cache_key = f'judge_health_{machine["name"]}'
                    cache.set(cache_key, False, 60)
                    logger.warning(
                        'Marked machine %s as unhealthy, falling back to default queue',
                        machine['name'],
                    )
            else:
                logger.warning('No healthy judge machines available, falling back to default queue')

        job = enqueue(judge_submission_task, submission_id)
        logger.info(
            'Enqueued judge task for submission %s to default queue, job ID: %s',
            submission_id, job.id,
        )
        return job
    except Exception:
        logger.exception('Failed to enqueue judge task for submission %s', submission_id)
        raise
