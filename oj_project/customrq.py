import signal
import sys
from django_rq.workers import get_worker
from rq.worker import Worker
import redis
import logging

from oj_project import settings

logger = logging.getLogger(__name__)

class AutoReconnectWorker(Worker):
    """
    A custom RQ worker that attempts to reconnect to Redis if the connection drops.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_retries = 5
        self.retry_delay = 5
        self._should_exit = False


    def _reconnect(self):
        """Attempt to re-establish the Redis connection."""
        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info(f"Attempting to reconnect to Redis (attempt {attempt})...")
                # Re-create the connection object
                self.connection = redis.Redis.from_url(self.redis_url)
                # Re-connect the worker's pubsub
                self.pubsub = self.connection.pubsub()
                logger.info("Successfully reconnected to Redis.")
                return
            except (redis.ConnectionError, redis.TimeoutError) as e:
                logger.error(f"Reconnection attempt {attempt} failed: {e}")
                time.sleep(self.retry_delay)
        # If all retries fail, raise a critical error to let the supervisor handle it
        raise redis.ConnectionError("Failed to reconnect to Redis after multiple attempts.")

    def _handle_exit(self, signum, frame):
        """Signal handler for SIGINT and SIGTERM."""
        logger.info("Received exit signal, shutting down gracefully...")
        self._should_exit = True
        # Optionally call super().shutdown() if you want RQ's graceful cleanup
        sys.exit(0)

    def work(self, *args, **kwargs):
        # Register signal handlers
        signal.signal(signal.SIGINT, self._handle_exit)
        signal.signal(signal.SIGTERM, self._handle_exit)

        while not self._should_exit:
            try:
                # Start the main work loop (this will block until worker stops)
                super().work(*args, **kwargs)
            except KeyboardInterrupt:
                logger.info("Ctrl+C pressed, exiting.")
                sys.exit(0)
            except (redis.ConnectionError, redis.TimeoutError) as e:
                logger.error(f"Redis connection lost: {e}. Attempting to reconnect...")
                self._reconnect()
                # After reconnecting, the loop continues
            except Exception as e:
                logger.exception(f"An unexpected error occurred: {e}")
                # For non-Redis errors, we exit so a fresh worker can take over.
                break

        logger.info("Worker loop ended.")

# Helper function to get your custom worker (optional, for use with `django-rq`)
def get_auto_reconnect_worker(*args, **kwargs):
    worker = AutoReconnectWorker(*args, **kwargs)
    worker.redis_url = settings.RQ_QUEUES['default']['URL']
    return worker