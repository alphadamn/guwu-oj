import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'oj_project.settings')

app = Celery('guwu_oj')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
