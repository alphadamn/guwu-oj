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
        # Check if multi-judge is enabled
        if getattr(settings, 'OJ_MULTI_JUDGE_ENABLED', False):
            # Use load balancer to select judge machine
            machine = load_balancer.select_machine()
            
            if machine:
                try:
                    # Increment busy count and record machine assignment
                    load_balancer._incr_busy(machine)
                    load_balancer._set_submission_machine(submission_id, machine['name'])
                    # Enqueue to specific judge machine's queue
                    queue_config = load_balancer.get_queue_for_machine(machine)
                    queue = get_queue(queue_config['name'])
                    job = queue.enqueue(judge_submission_task, submission_id)
                    logger.info('Enqueued judge task for submission %s to machine %s, job ID: %s', 
                               submission_id, machine['name'], job.id)
                    return job
                except Exception as e:
                    # If enqueue fails, mark machine as unhealthy and fallback
                    logger.error(f'Failed to enqueue to machine {machine["name"]}: {e}')
                    cache_key = f'judge_health_{machine["name"]}'
                    cache.set(cache_key, False, 60)  # Mark as unhealthy for 60 seconds
                    logger.warning('Marked machine %s as unhealthy, falling back to default queue', machine['name'])
            else:
                # Fallback to default queue if no healthy machines
                logger.warning('No healthy judge machines available, falling back to default queue')
        
        # Default behavior: enqueue to default queue
        job = enqueue(judge_submission_task, submission_id)
        logger.info('Enqueued judge task for submission %s to default queue, job ID: %s', 
                   submission_id, job.id)
        return job
    except Exception:
        logger.exception('Failed to enqueue judge task for submission %s', submission_id)
        raise
