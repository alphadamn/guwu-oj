"""Publish a Redis notification whenever a submission or one of its test
results is written.

Hooking ``post_save`` (instead of calling the publisher from the judge loop)
covers every status transition: per-case results, compile/system errors,
final verdict, and even admin-side edits. The WebSocket endpoint re-reads
the database after each ping, so the message itself carries no payload.
"""

import logging

from django.db.models.signals import post_save

from .models import Submission, SubmissionTestResult
from .realtime import publish_submission_changed

logger = logging.getLogger(__name__)


def submission_saved(sender, instance, **kwargs):
    publish_submission_changed(instance.id)


def test_result_saved(sender, instance, **kwargs):
    publish_submission_changed(instance.submission_id)


def connect_signals():
    post_save.connect(submission_saved, sender=Submission, dispatch_uid='oj.submission.ws')
    post_save.connect(
        test_result_saved, sender=SubmissionTestResult, dispatch_uid='oj.testresult.ws'
    )
