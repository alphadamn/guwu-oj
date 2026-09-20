"""Internal API consumed by DB-less judge workers (Phase 3).

Endpoints (shared-secret auth via ``X-Judge-Token``):

* ``POST /internal/judge/claim/`` — atomic claim + everything the worker
  needs to judge (source, limits, test data) in one round trip.
* ``POST /internal/judge/heartbeat/`` — lease renewal.

These endpoints never set cookies and are CSRF-exempt (machine auth, not
browser auth). They are intentionally kept off the public API surface:
they carry full test data and must only be reachable by judge machines
(network-level restriction can be added at nginx in addition to the token).
"""

from __future__ import annotations

import hmac
import json
import logging

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .claiming import claim_submission, heartbeat_claim
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
    if problem is None:
        cases = []
        time_limit_ms = None
        memory_limit_mb = None
    else:
        cases_qs = problem.test_cases.order_by('order', 'id')
        cases = [
            {
                'index': idx,
                'input': tc.input_data,
                'expected': tc.expected_output,
            }
            for idx, tc in enumerate(cases_qs, start=1)
        ]
        time_limit_ms = problem.time_limit
        memory_limit_mb = problem.memory_limit

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
        'cases': cases,
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
