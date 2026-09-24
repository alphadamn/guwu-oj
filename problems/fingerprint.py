"""Content fingerprint of a problem's test data (the judge cache key).

A DB-less judge worker keeps the test data it downloads on local disk so the
next submission of the same problem is judged without re-pulling tens of
megabytes over the (narrow) web uplink. That cache has to be keyed on the
content, not on the problem id: re-importing a problem replaces every test
case, and serving the previous data would produce wrong verdicts.

So the web side publishes a sha256 over the ordered ``(id, input, expected)``
stream. Digesting a fat problem means reading all of its test data (a large
problem is ~70 MB), which is why the result is memoised in the shared cache:

* ``TestCase`` writes through the ORM invalidate the entry from ``post_save``
  / ``post_delete``;
* the importer scripts insert with ``bulk_create``, which fires no signals, so
  the entry is additionally re-validated on read against a cheap shape
  aggregate (case count / max id / order sum) that any insert or delete moves.

The empty fingerprint means "this problem has no test data"; callers treat it
as nothing to cache.
"""

from __future__ import annotations

import hashlib
import logging

from django.core.cache import cache
from django.db.models import Count, Max, Sum
from django.db.models.signals import post_delete, post_save

from .models import Problem, TestCase

logger = logging.getLogger(__name__)

CACHE_KEY = 'oj:testdata:fp:{problem_id}'
# Invalidation is explicit, so the TTL only bounds Redis memory for problems
# nobody submits to again.
CACHE_TTL = 60 * 60 * 24 * 30


def _shape(problem_id):
    """Metadata that moves on any test-case insert or delete.

    Also folds in a digest of the problem's ``function_files`` so a grader
    edit (which leaves the TestCase rows untouched) still invalidates the
    cached fingerprint — without it, a worker would judge a fresh submission
    against the previous grader.
    """
    agg = TestCase.objects.filter(problem_id=problem_id).aggregate(
        count=Count('id'),
        max_id=Max('id'),
        order_sum=Sum('order'),
    )
    func_files = (
        Problem.objects
        .filter(pk=problem_id)
        .values_list('function_files', flat=True)
        .first()
    ) or ''
    return (
        agg['count'] or 0,
        agg['max_id'] or 0,
        agg['order_sum'] or 0,
        hashlib.sha256(func_files.encode('utf-8', 'surrogatepass')).hexdigest(),
    )


def _digest(problem_id):
    """sha256 over the test data, streamed through a server-side cursor."""
    digest = hashlib.sha256()
    rows = (
        TestCase.objects
        .filter(problem_id=problem_id)
        .order_by('order', 'id')
        .values_list('id', 'input_data', 'expected_output')
        .iterator(chunk_size=8)
    )
    for case_id, input_data, expected_output in rows:
        # The index a worker files a case under is its position in this
        # stream, so the row id and the separators are part of the hash: two
        # problems with the same text in a different split must not collide.
        digest.update(b'%d\0' % case_id)
        digest.update((input_data or '').encode('utf-8', 'surrogatepass'))
        digest.update(b'\0')
        digest.update((expected_output or '').encode('utf-8', 'surrogatepass'))
        digest.update(b'\0')
    # Function-style problems: the grader/header files are part of the
    # judging contract, so they belong in the worker cache key. A grader
    # change with identical test data must invalidate the cached payload.
    func_files = (
        Problem.objects
        .filter(pk=problem_id)
        .values_list('function_files', flat=True)
        .first()
    ) or ''
    digest.update(b'files\0')
    digest.update(func_files.encode('utf-8', 'surrogatepass'))
    digest.update(b'\0')
    return digest.hexdigest()


def _memoised(key):
    try:
        return cache.get(key)
    except Exception:
        logger.warning('Could not read test data fingerprint cache', exc_info=True)
        return None


def compute_test_data_fingerprint(problem_id):
    """Return a stable key for the problem's current test data.

    Empty string when the problem has no test cases. Best effort on the cache:
    a cache outage costs a re-digest, never a request failure.
    """
    key = CACHE_KEY.format(problem_id=problem_id)
    shape = _shape(problem_id)
    if shape[0] == 0:
        return ''
    memo = _memoised(key)
    if memo and tuple(memo.get('shape') or ()) == shape:
        return memo.get('fp') or ''
    fingerprint = _digest(problem_id)
    try:
        cache.set(key, {'fp': fingerprint, 'shape': shape}, CACHE_TTL)
    except Exception:
        logger.warning(
            'Could not memoise test data fingerprint for problem %s',
            problem_id, exc_info=True,
        )
    return fingerprint


def invalidate_test_data_fingerprint(problem_id):
    """Drop the memoised fingerprint after a test-case write."""
    if not problem_id:
        return
    try:
        cache.delete(CACHE_KEY.format(problem_id=problem_id))
    except Exception:
        logger.warning(
            'Could not invalidate test data fingerprint for problem %s',
            problem_id, exc_info=True,
        )


def _test_case_changed(sender, instance, **kwargs):
    invalidate_test_data_fingerprint(instance.problem_id)


def _problem_changed(sender, instance, **kwargs):
    # A grader/header edit on a function problem leaves TestCase rows alone,
    # so the TestCase signal would not fire. The Problem save itself must
    # invalidate the cached fingerprint or workers would judge with a stale
    # grader until the shape mismatch re-digests on the next read.
    invalidate_test_data_fingerprint(getattr(instance, 'id', None) or instance.pk)


def connect_signals():
    post_save.connect(
        _test_case_changed, sender=TestCase,
        dispatch_uid='oj.testdata.fp.saved',
    )
    post_delete.connect(
        _test_case_changed, sender=TestCase,
        dispatch_uid='oj.testdata.fp.deleted',
    )
    post_save.connect(
        _problem_changed, sender=Problem,
        dispatch_uid='oj.testdata.fp.problem.saved',
    )
