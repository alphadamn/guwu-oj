"""Publish a Redis notification whenever a submission or one of its test
results is written.

Hooking ``post_save`` (instead of calling the publisher from the judge loop)
covers every status transition: per-case results, compile/system errors,
final verdict, and even admin-side edits. The WebSocket endpoint re-reads
the database after each ping, so the message itself carries no payload.
"""

import logging

from django.db import transaction
from django.db.models.signals import post_save

from .models import Submission, SubmissionTestResult
from .realtime import publish_submission_changed

logger = logging.getLogger(__name__)


def _publish_on_commit(submission_id):
    """Publish only after the surrounding transaction commits.

    A mid-transaction publish makes the WebSocket consumer re-read the
    database immediately and see the pre-commit state; that stale fetch
    then resets the push coalesce window, so the authoritative post-commit
    notification gets swallowed and the UI falls back to the slow watchdog.
    Outside an atomic block ``on_commit`` fires immediately.
    """
    transaction.on_commit(lambda: publish_submission_changed(submission_id))


def submission_saved(sender, instance, **kwargs):
    _publish_on_commit(instance.id)


def test_result_saved(sender, instance, **kwargs):
    _publish_on_commit(instance.submission_id)


def connect_signals():
    post_save.connect(submission_saved, sender=Submission, dispatch_uid='oj.submission.ws')
    post_save.connect(
        test_result_saved, sender=SubmissionTestResult, dispatch_uid='oj.testresult.ws'
    )
