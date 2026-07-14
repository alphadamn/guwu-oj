import logging
import threading
import time

from submissions.docker_cleanup import cleanup_stale_judge_containers

logger = logging.getLogger(__name__)

_started = False
_start_lock = threading.Lock()


def _cleanup_loop():
    while True:
        cleanup_stale_judge_containers()
        time.sleep(15)


def start_container_cleanup():
    """Start judge container cleanup daemon (RQ worker only)."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    thread = threading.Thread(target=_cleanup_loop, daemon=True, name='container-cleanup')
    thread.start()
    logger.info('Judge container cleanup daemon started')
