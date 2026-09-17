import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from submissions.container_cleanup import start_container_cleanup
from submissions.container_pool import shutdown_pool, start_pool

import redis
from django.conf import settings
from rq import SimpleWorker
from rq.timeouts import TimerDeathPenalty

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SEC = 30
HEARTBEAT_TTL_SEC = 90
DEFAULT_CONCURRENCY = 4
SLOT_WAIT_POLL_SEC = 1.0


def _resolve_concurrency():
    """Parallel judgements handled by a single worker process."""
    raw = getattr(settings, 'OJ_JUDGE_CONCURRENCY', DEFAULT_CONCURRENCY)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_CONCURRENCY
    return max(1, value)


class AutoReconnectWorker(SimpleWorker):
    """RQ worker that judges multiple submissions concurrently.

    Jobs run in a bounded thread pool instead of the classic fork-per-job
    model, so one ``rqworker`` process on a judge machine can execute
    ``OJ_JUDGE_CONCURRENCY`` (default 4) submissions at the same time.
    Judging only shells out to Docker and waits on subprocesses, which makes
    threads a safe and effective execution model. ``TimerDeathPenalty``
    replaces the default SIGALRM penalty, which only works in the main
    thread.
    """

    death_penalty_class = TimerDeathPenalty

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_retries = 5
        self.retry_delay = 5
        self.concurrency = _resolve_concurrency()
        self._stop_heartbeat = threading.Event()
        self._connection_kwargs = None
        # Slots gate how many jobs may be dequeued but not finished yet.
        self._job_slots = threading.BoundedSemaphore(self.concurrency)
        self._executor = ThreadPoolExecutor(
            max_workers=self.concurrency,
            thread_name_prefix='judge-job',
        )
        start_container_cleanup()
        # Warm the per-language judge container pool in the background so
        # the first submissions after (re)start pay no docker-run overhead.
        start_pool()
        logger.info(
            'Judge worker %s started with concurrency %s', self.name, self.concurrency
        )

    def _queue_names(self):
        if self.queues:
            return [q.name for q in self.queues]
        return ['default']

    def _refresh_heartbeat(self):
        queue_names = self._queue_names()
        for queue_name in queue_names:
            key = f'judge:worker:{queue_name}'
            self.connection.set(key, int(time.time()), ex=HEARTBEAT_TTL_SEC)
        # Keep the RQ worker record alive while long judge jobs run in
        # threads. The forking worker got this from monitor_work_horse().
        # heartbeat() hsets the worker key into existence, so it must not
        # run before register_birth() or bootstrap would see a stale record.
        if getattr(self, 'birth_date', None) is not None:
            try:
                self.heartbeat()
            except (redis.ConnectionError, redis.TimeoutError) as exc:
                logger.warning('RQ heartbeat refresh failed: %s', exc)

    def _heartbeat_loop(self):
        while not self._stop_heartbeat.wait(HEARTBEAT_INTERVAL_SEC):
            try:
                self._refresh_heartbeat()
            except (redis.ConnectionError, redis.TimeoutError) as exc:
                logger.warning('Heartbeat refresh failed: %s', exc)

    def _start_heartbeat(self):
        self._stop_heartbeat.clear()
        self._refresh_heartbeat()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True,
            name='judge-worker-heartbeat',
        )
        self._heartbeat_thread.start()

    def _stop_heartbeat_thread(self):
        self._stop_heartbeat.set()
        if hasattr(self, '_heartbeat_thread') and self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2)

    def _connection_options(self):
        if self._connection_kwargs is None:
            self._connection_kwargs = dict(self.connection.connection_pool.connection_kwargs)
        return dict(self._connection_kwargs)

    def _reconnect(self):
        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info('Attempting to reconnect to Redis (attempt %s)...', attempt)
                connection = redis.Redis(**self._connection_options())
                connection.ping()
                self.connection = connection
                self.pubsub = connection.pubsub()
                logger.info('Successfully reconnected to Redis.')
                return
            except (redis.ConnectionError, redis.TimeoutError) as exc:
                logger.error('Reconnection attempt %s failed: %s', attempt, exc)
                time.sleep(self.retry_delay)
        raise redis.ConnectionError('Failed to reconnect to Redis after multiple attempts.')

    def dequeue_job_and_maintain_ttl(self, timeout, max_idle_time=None):
        """Gate dequeuing on free concurrency slots.

        Acquiring a slot *before* the blocking dequeue guarantees that a
        popped job always has a worker thread ready for it. While every
        slot is busy, keep heartbeating so the worker record does not
        expire under long judgements.
        """
        while not self._job_slots.acquire(timeout=SLOT_WAIT_POLL_SEC):
            if self._stop_requested:
                raise StopRequested()
            try:
                self.heartbeat()
            except (redis.ConnectionError, redis.TimeoutError) as exc:
                logger.warning('Heartbeat while waiting for a judge slot failed: %s', exc)
        try:
            return super().dequeue_job_and_maintain_ttl(timeout, max_idle_time)
        except BaseException:
            self._job_slots.release()
            raise

    def execute_job(self, job, queue):
        """Dispatch the dequeued job to the thread pool without blocking."""
        self.prepare_execution(job)
        try:
            self._executor.submit(self._perform_job_in_thread, job, queue)
        except BaseException:
            self._job_slots.release()
            raise

    def _perform_job_in_thread(self, job, queue):
        try:
            from django.db import close_old_connections
            close_old_connections()
        except Exception:
            pass
        try:
            self.perform_job(job, queue)
        except Exception:
            # perform_job() handles job-level failures itself; anything
            # escaping here is a worker-level error (e.g. Redis outage).
            logger.exception('Worker %s: job %s failed outside perform_job', self.name, job.id)
        finally:
            try:
                from django.db import close_old_connections
                close_old_connections()
            except Exception:
                pass
            self._job_slots.release()

    def work(self, *args, **kwargs):
        self._connection_options()
        self._start_heartbeat()
        try:
            while True:
                try:
                    super().work(*args, **kwargs)
                    break
                except (redis.ConnectionError, redis.TimeoutError) as exc:
                    logger.error('Redis connection lost: %s. Attempting to reconnect...', exc)
                    self._reconnect()
                    # register_birth() refuses to re-register while our own
                    # record is still around from before the disconnect.
                    try:
                        self.connection.delete(self.key)
                    except (redis.ConnectionError, redis.TimeoutError):
                        pass
                except Exception:
                    logger.exception('An unexpected error occurred in judge worker')
                    break
        finally:
            self._stop_heartbeat_thread()
            # Wait for in-flight judges before the process exits so a
            # graceful stop never truncates submissions mid-judgement.
            self._executor.shutdown(wait=True)
            # All judges have returned their pool containers; tear the pool
            # down so no warm containers are orphaned.
            shutdown_pool()
            # super().work() skips teardown when bootstrap fails (e.g. a
            # stale worker record); close out our registration regardless.
            try:
                self.teardown()
            except Exception:
                logger.exception('Failed to tear down worker %s cleanly', self.name)


def get_auto_reconnect_worker(*args, **kwargs):
    from django.conf import settings

    worker = AutoReconnectWorker(*args, **kwargs)
    worker.redis_url = settings.RQ_QUEUES['default']['URL']
    return worker
