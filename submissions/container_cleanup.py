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

def start_container_cleanup():
    """Start a daemon thread that runs _cleanup_once every 15 seconds."""
    def _run():
        while True:
            _cleanup_once()
            time.sleep(15)
    thread = threading.Thread(target=_run, daemon=True, name="container-cleanup")
    thread.start()
