import os
import fcntl
import threading
import json
import threading
import subprocess
import time
from datetime import datetime, timezone
from dateutil import parser
import logging

logger = logging.getLogger(__name__)

def _cleanup_once():
    """Inspect running Docker containers and kill those running >= 5 seconds."""
    try:
        list_res = subprocess.run(
            ["docker", "ps", "-q"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for cid in list_res.stdout.strip().splitlines():
            try:
                inspect_res = subprocess.run(
                    ["docker", "inspect", cid],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if inspect_res.returncode != 0:
                    continue
                data = json.loads(inspect_res.stdout)
                started_at_str = data[0]["State"]["StartedAt"]
                started_at = parser.isoparse(started_at_str.replace('Z', '+00:00'))
                now = datetime.now(timezone.utc)
                running_seconds = (now - started_at).total_seconds()
                if running_seconds >= 5:
                    print('cleaned up container', cid)
                    subprocess.run(["docker", "kill", cid], capture_output=True, text=True)
            except Exception as e:
                logger.exception("Error processing container %s", cid)
    except Exception:
        # Silent failure to match original behavior
        pass

class DockerCleanupThread(threading.Thread):
    """
    A daemon thread that runs the Docker cleanup routine periodically.
    Uses a file-based inter-process lock to ensure only ONE thread runs
    across all Django workers/processes on the same machine.
    """

    def __init__(
        self,
        interval: int = 5,
        lock_file: str = "/tmp/docker_cleanup.lock",
        daemon: bool = True,
    ):
        """
        Args:
            interval: How often to run the cleanup (seconds).
            lock_file: Absolute path to the lock file.
            daemon: If True, the thread exits when the main process exits.
        """
        super().__init__(daemon=daemon)
        self.interval = interval
        self.lock_file_path = lock_file
        self._stop_event = threading.Event()
        self._lock_fd = None

    def _acquire_global_lock(self) -> bool:
        """
        Try to acquire an exclusive, non-blocking file lock.
        Returns True if this process owns the lock, False otherwise.
        """
        try:
            # Open (or create) the lock file
            self._lock_fd = open(self.lock_file_path, "w")
            # LOCK_EX = exclusive lock, LOCK_NB = non-blocking
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (IOError, OSError):
            # Another process already holds the lock
            if self._lock_fd:
                self._lock_fd.close()
                self._lock_fd = None
            return False

    def _release_global_lock(self) -> None:
        """Release the file lock if held."""
        if self._lock_fd:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                self._lock_fd.close()
            except Exception as e:
                logger.warning("Error releasing lock: %s", e)
            finally:
                self._lock_fd = None

    def run(self) -> None:
        """
        Main entry point for the thread. Called when you call .start().
        """
        # Attempt to become the single global instance
        if not self._acquire_global_lock():
            print(f"🔒 Docker cleanup lock held by another process. Thread exiting.")
            logger.info("DockerCleanupThread: lock held elsewhere, exiting.")
            return

        print(f"🔓 Docker cleanup lock acquired. Running every {self.interval}s.")
        logger.info(f"DockerCleanupThread started with interval={self.interval}s")

        try:
            while not self._stop_event.is_set():
                # Execute the actual cleanup
                try:
                    _cleanup_once()
                except Exception as e:
                    # Your function already logs internally, but catch any unexpected
                    logger.exception("Unhandled error in cleanup loop")

                # Sleep in 1-second increments to respond quickly to stop()
                for _ in range(self.interval):
                    if self._stop_event.is_set():
                        break
                    time.sleep(1)
        finally:
            # Critical: release the global lock so a new instance can take over
            self._release_global_lock()
            print("🛑 DockerCleanupThread stopped. Lock released.")
            logger.info("DockerCleanupThread stopped.")

    def stop(self) -> None:
        """
        Gracefully stop the thread. The current cleanup run will finish,
        and the loop will exit before the next sleep.
        """
        self._stop_event.set()
        # Optionally, we could interrupt sleep by sending a signal,
        # but sleeping in 1s chunks is good enough.

