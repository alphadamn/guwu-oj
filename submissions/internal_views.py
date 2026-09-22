"""Internal API consumed by DB-less judge workers (Phase 3).

Endpoints (shared-secret auth via ``X-Judge-Token``):

* ``POST /internal/judge/claim/`` — atomic claim + everything the worker
  needs to judge (source, limits, how many test cases exist). Test data is
  NOT inlined for workers that ask for ``batched_cases``: see below.
* ``POST /internal/judge/cases/`` — one slice of test data for a claim the
  caller still owns. Kept separate from the claim so judging can start
  while a multi-hundred-megabyte payload is still streaming in.
* ``POST /internal/judge/heartbeat/`` — lease renewal.
* ``POST /internal/judge/report_ip/`` — record the caller's edge IP and
  refresh the direct-link firewall (workers behind dynamic NAT).

These endpoints never set cookies and are CSRF-exempt (machine auth, not
browser auth). They are intentionally kept off the public API surface:
they carry full test data and must only be reachable by judge machines
(network-level restriction can be added at nginx in addition to the token).
"""

from __future__ import annotations

import hmac
import json
import logging
import uuid

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from problems.fingerprint import compute_test_data_fingerprint

from .claiming import claim_submission, heartbeat_claim
from .judge_firewall import apply_firewall, is_usable_source, record_worker_ip
from .models import Submission

logger = logging.getLogger(__name__)


def _json_body(request):
    try:
        return json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return None


def _authenticated(request):
    expected = getattr(settings, 'JUDGE_INTERNAL_TOKEN', '') or ''
    provided = request.headers.get('X-Judge-Token', '')
    return bool(expected) and bool(provided) and hmac.compare_digest(
        expected, provided,
    )


def _unauthorized():
    return JsonResponse({'error': 'invalid token'}, status=403)


def _observed_source_ip(request):
    """Best-effort client IP as seen at the edge.

    The nginx edge overwrites ``X-Forwarded-For`` with the real client (from
    Cloudflare's ``CF-Connecting-IP``, or the TCP peer on the direct link),
    so it is authoritative here; the remaining headers are fallbacks.
    """
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    for header in ('HTTP_CF_CONNECTING_IP', 'HTTP_X_REAL_IP'):
        value = (request.META.get(header) or '').strip()
        if value:
            return value
    return (request.META.get('REMOTE_ADDR') or '').strip()


@csrf_exempt
@require_POST
def claim_view(request):
    if not _authenticated(request):
        return _unauthorized()
    body = _json_body(request)
    if body is None:
        return JsonResponse({'error': 'invalid json'}, status=400)

    submission_id = body.get('submission_id')
    worker_id = (body.get('worker_id') or '')[:128]
    if not submission_id or not worker_id:
        return JsonResponse(
            {'error': 'submission_id and worker_id required'}, status=400,
        )
    # A worker that advertises batched support pulls test data on demand
    # through ``cases_view``; inlining it here would stall ``claim`` behind
    # a single response that can reach hundreds of megabytes.
    batched_cases = bool(body.get('batched_cases'))

    token = claim_submission(submission_id, worker_id)
    if token is None:
        # Duplicate delivery / already terminal: worker ACKs the job.
        return JsonResponse({'claimable': False}, status=409)

    try:
        submission = (
            Submission.objects
            .select_related('problem', 'contest_problem')
            .get(pk=submission_id)
        )
    except Submission.DoesNotExist:
        return JsonResponse({'claimable': False, 'reason': 'missing'},
                            status=409)

    problem = submission.effective_problem
    data_key = ''
    if problem is None:
        cases = []
        total_cases = 0
        time_limit_ms = None
        memory_limit_mb = None
    else:
        cases_qs = problem.test_cases.order_by('order', 'id')
        total_cases = cases_qs.count()
        cases = [] if batched_cases else [
            {
                'index': idx,
                'input': tc.input_data,
                'expected': tc.expected_output,
            }
            for idx, tc in enumerate(cases_qs, start=1)
        ]
        time_limit_ms = problem.time_limit
        memory_limit_mb = problem.memory_limit
        if batched_cases:
            # Lets the worker keep its own copy of this problem's test data
            # and skip the download on the next submission. Only batched
            # workers pay for it: the digest reads every case.
            data_key = compute_test_data_fingerprint(problem.id)

    try:
        from .models import JudgeConfig
        subprocess_timeout_sec = int(
            JudgeConfig.get_solo().subprocess_timeout_sec
        )
    except Exception:
        subprocess_timeout_sec = int(
            getattr(settings, 'OJ_SUBPROCESS_TIMEOUT_SEC', 5) or 5
        )

    return JsonResponse({
        'claimable': True,
        'claim_token': str(token),
        'submission_id': submission.id,
        'language': submission.language,
        'code': submission.code,
        'user_id': submission.user_id,
        'is_contest': submission.contest_problem_id is not None,
        'time_limit_ms': time_limit_ms,
        'memory_limit_mb': memory_limit_mb,
        'subprocess_timeout_sec': subprocess_timeout_sec,
        'total_cases': total_cases,
        # Content hash of the test data: the worker caches its local copy
        # under this key and re-downloads only when it changes.
        'data_key': data_key,
        'cases': cases,
    })


# Test cases served per batched fetch. Small enough that the first case can
# start almost immediately, large enough to keep round trips off the hot path.
DEFAULT_CASE_BATCH = 8
MAX_CASE_BATCH = 64


@csrf_exempt
@require_POST
def cases_view(request):
    """Serve a slice of test data to the worker that owns the claim.

    The caller proves ownership with its claim token, so a revoked or
    superseded worker cannot keep pulling data (and gets a ``409``, which
    its client maps to :class:`~submissions.worker_api.ClaimLostError`).
    """
    if not _authenticated(request):
        return _unauthorized()
    body = _json_body(request)
    if body is None:
        return JsonResponse({'error': 'invalid json'}, status=400)

    submission_id = body.get('submission_id')
    token_raw = body.get('claim_token')
    if not submission_id or not token_raw:
        return JsonResponse(
            {'error': 'submission_id and claim_token required'}, status=400,
        )
    try:
        token = uuid.UUID(str(token_raw))
    except (TypeError, ValueError):
        return JsonResponse({'error': 'invalid claim_token'}, status=400)
    try:
        offset = max(int(body.get('offset') or 0), 0)
        limit = int(body.get('limit') or DEFAULT_CASE_BATCH)
    except (TypeError, ValueError):
        return JsonResponse(
            {'error': 'offset and limit must be integers'}, status=400,
        )
    limit = min(max(limit, 1), MAX_CASE_BATCH)

    submission = (
        Submission.objects
        .select_related('problem', 'contest_problem')
        .filter(pk=submission_id, claim_token=token, judge_state='JUDGING')
        .first()
    )
    if submission is None:
        return JsonResponse({'claimable': False}, status=409)

    problem = submission.effective_problem
    if problem is None:
        return JsonResponse({'offset': offset, 'total': 0, 'cases': []})

    cases_qs = problem.test_cases.order_by('order', 'id')
    rows = list(cases_qs[offset:offset + limit])
    return JsonResponse({
        'offset': offset,
        'total': cases_qs.count(),
        'cases': [
            {
                'index': offset + i,
                'input': tc.input_data,
                'expected': tc.expected_output,
            }
            for i, tc in enumerate(rows, start=1)
        ],
    })


@csrf_exempt
@require_POST
def heartbeat_view(request):
    if not _authenticated(request):
        return _unauthorized()
    body = _json_body(request)
    if body is None:
        return JsonResponse({'error': 'invalid json'}, status=400)

    submission_id = body.get('submission_id')
    token = body.get('claim_token')
    if not submission_id or not token:
        return JsonResponse(
            {'error': 'submission_id and claim_token required'}, status=400,
        )
    alive = heartbeat_claim(submission_id, token)
    return JsonResponse({'alive': alive})


@csrf_exempt
@require_POST
def report_ip_view(request):
    """Record the caller's edge IP and refresh the direct-link firewall.

    Workers behind dynamic NAT call this over the always-reachable CDN
    origin; the reported IP is taken from the connection rather than the
    body, so a worker cannot whitelist an arbitrary address. Failures to
    touch iptables are reported but not retried by the worker (the periodic
    timer re-runs the sync anyway).
    """
    if not _authenticated(request):
        return _unauthorized()
    body = _json_body(request)
    if body is None:
        return JsonResponse({'error': 'invalid json'}, status=400)

    worker_id = (body.get('worker_id') or '')[:128].strip()
    if not worker_id:
        return JsonResponse({'error': 'worker_id required'}, status=400)

    ip = _observed_source_ip(request)
    if not is_usable_source(ip):
        logger.warning(
            'Worker %s reported unusable source IP %r', worker_id, ip,
        )
        return JsonResponse(
            {'error': 'source address not usable for direct link',
             'observed_ip': ip},
            status=400,
        )

    previous = record_worker_ip(worker_id, ip)
    allowed = apply_firewall()

    if previous != ip:
        logger.info(
            'Worker %s direct-link source IP %s -> %s',
            worker_id, previous or '(none)', ip,
        )

    return JsonResponse({
        'ok': True,
        'worker_id': worker_id,
        'ip': ip,
        'previous_ip': previous,
        'applied': allowed is not None,
    })
