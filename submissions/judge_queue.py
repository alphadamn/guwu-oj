import logging

from django_rq import enqueue
from rq import Queue
from django.conf import settings
from django.core.cache import cache

from .tasks import judge_submission_task
from .judge_load_balancer import load_balancer

logger = logging.getLogger(__name__)


def enqueue_judge(submission_id):
    """Enqueue judge task to RQ queue for async execution with load balancing."""
    try:
        if getattr(settings, 'OJ_MULTI_JUDGE_ENABLED', False):
            machine = load_balancer.reserve_machine(submission_id)

            if machine:
                try:
                    queue = Queue(
                        machine['queue'], connection=load_balancer._machine_redis(machine)
                    )
                    job = queue.enqueue(
                        judge_submission_task,
                        submission_id,
                        meta={
                            'judge_machine': machine['name'],
                            'submission_id': submission_id,
                        },
                    )
                    logger.info(
                        'Enqueued judge task for submission %s to machine %s, job ID: %s',
                        submission_id, machine['name'], job.id,
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

        job = enqueue(judge_submission_task, submission_id)
        # django-rq >= 4 returns None when the enqueue is deferred to the
        # database commit (COMMIT_MODE 'on_db_commit') or to the end of the
        # request ('request_finished'). The job is still queued, just later.
        logger.info(
            'Enqueued judge task for submission %s to default queue, job ID: %s',
            submission_id, getattr(job, 'id', 'deferred'),
        )
        return job
    except Exception:
        logger.exception('Failed to enqueue judge task for submission %s', submission_id)
        raise
