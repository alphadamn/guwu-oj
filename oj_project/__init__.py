# Expose the Celery app so `celery -A oj_project worker` and Django share it.
from .celery import app as celery_app

__all__ = ('celery_app',)
