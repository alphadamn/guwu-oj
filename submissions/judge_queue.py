import logging
import threading

from django.db import close_old_connections

from .judge import judge_submission

logger = logging.getLogger(__name__)


def enqueue_judge(submission_id):
    """Run judge in a background thread so HTTP responses are not blocked."""

    def _run():
        close_old_connections()
        try:
            judge_submission(submission_id)
        except Exception:
            logger.exception('Judge failed for submission %s', submission_id)
        finally:
            close_old_connections()

    thread = threading.Thread(target=_run, daemon=True, name=f'oj-judge-{submission_id}')
    thread.start()
