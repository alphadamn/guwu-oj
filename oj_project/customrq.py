import sys
import os
import logging
import threading
import time
import redis
from rq.worker import SimpleWorker  # 改为继承 SimpleWorker

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SEC = 30
HEARTBEAT_TTL_SEC = 90

# Windows 信号宏补丁（SimpleWorker 不会用到，但保留无害）
if sys.platform == 'win32':
    if not hasattr(os, 'WIFSIGNALED'):
        os.WIFSIGNALED = lambda status: False
    if not hasattr(os, 'WTERMSIG'):
        os.WTERMSIG = lambda status: 0
    if not hasattr(os, 'WIFEXITED'):
        os.WIFEXITED = lambda status: True
    if not hasattr(os, 'WEXITSTATUS'):
        os.WEXITSTATUS = lambda status: status

class AutoReconnectWorker(SimpleWorker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_retries = 5
        self.retry_delay = 5
        self._stop_heartbeat = threading.Event()
        self._connection_kwargs = None

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

    def work(self, *args, **kwargs):
        """重写 work 方法以支持自动重连（SimpleWorker 在主进程执行任务）"""
        self._connection_options()
        self._start_heartbeat()
        try:
            while True:
                try:
                    # SimpleWorker.work() 会循环处理任务，直到队列为空或发生异常
                    super().work(*args, **kwargs)
                    # 如果正常结束（比如队列为空），退出循环
                    break
                except (redis.ConnectionError, redis.TimeoutError) as exc:
                    logger.error('Redis connection lost: %s. Attempting to reconnect...', exc)
                    self._reconnect()
                    # 重连后继续循环
                except Exception as e:
                    logger.exception('An unexpected error occurred in judge worker: %s', e)
                    # 根据业务决定是否继续
                    break
        finally:
            self._stop_heartbeat_thread()


def get_auto_reconnect_worker(*args, **kwargs):
    from django.conf import settings
    worker = AutoReconnectWorker(*args, **kwargs)
    worker.redis_url = settings.RQ_QUEUES['default']['URL']
    return worker