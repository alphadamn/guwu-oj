import logging
import threading
import time

from submissions.container_cleanup import start_container_cleanup

import redis
from rq.worker import Worker

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SEC = 30
HEARTBEAT_TTL_SEC = 90


class AutoReconnectWorker(Worker):
    """RQ worker with auto-reconnect and judge heartbeat."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_retries = 5
        self.retry_delay = 5
        self._stop_heartbeat = threading.Event()
        self.redis_url = None
        start_container_cleanup()

    def _queue_names(self):
        if self.queues:
            return [q.name for q in self.queues]
        return ['default']

    def _refresh_heartbeat(self):
        queue_names = self._queue_names()
        for queue_name in queue_names:
            key = f'judge:worker:{queue_name}'
            self.connection.set(key, int(time.time()), ex=HEARTBEAT_TTL_SEC)

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

    def _ensure_redis_url(self):
        if getattr(self, 'redis_url', None):
            return
        pool = self.connection.connection_pool
        kw = pool.connection_kwargs
        password = kw.get('password')
        host = kw.get('host', 'localhost')
        port = kw.get('port', 6379)
        db = kw.get('db', 0)
        if password:
            self.redis_url = f'redis://:{password}@{host}:{port}/{db}'
        else:
            self.redis_url = f'redis://{host}:{port}/{db}'

    def _reconnect(self):
        self._ensure_redis_url()
        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info('Attempting to reconnect to Redis (attempt %s)...', attempt)
                self.connection = redis.Redis.from_url(self.redis_url)
                self.pubsub = self.connection.pubsub()
                logger.info('Successfully reconnected to Redis.')
                return
            except (redis.ConnectionError, redis.TimeoutError) as exc:
                logger.error('Reconnection attempt %s failed: %s', attempt, exc)
                time.sleep(self.retry_delay)
        raise redis.ConnectionError('Failed to reconnect to Redis after multiple attempts.')

    def work(self, *args, **kwargs):
        self._ensure_redis_url()
        self._start_heartbeat()
        try:
            while True:
                try:
                    super().work(*args, **kwargs)
                    break
                except (redis.ConnectionError, redis.TimeoutError) as exc:
                    logger.error('Redis connection lost: %s. Attempting to reconnect...', exc)
                    self._reconnect()
                except Exception:
                    logger.exception('An unexpected error occurred in judge worker')
                    break
        finally:
            self._stop_heartbeat_thread()


def get_auto_reconnect_worker(*args, **kwargs):
    from django.conf import settings

    worker = AutoReconnectWorker(*args, **kwargs)
    worker.redis_url = settings.RQ_QUEUES['default']['URL']
    return worker
