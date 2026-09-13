"""Backend for the model-facing ``submit_to_judge`` tool.

When the AI explains a problem it may verify its approach by submitting a
complete reference program to the *real* judge. The submission is made under
a dedicated, non-login service account (never the asking user's account,
never a staff account):

* keeps the user's own submission history / statistics clean;
* makes the verification runs auditable (filter Submission by that user);
* lets the normal RQ judge pipeline be reused unchanged.

The caller (``deepseek_api.stream_answer``) enforces the per-generation call
cap; this module only validates arguments, creates the row, enqueues the
judge and waits for the verdict.
"""
from __future__ import annotations

import json
import logging
import time

from django.conf import settings
from django.db import transaction

from submissions.judge import JUDGED_LANGUAGES
from submissions.judge_queue import enqueue_judge
from submissions.models import Submission, SubmissionTestResult
from users.models import User

from .constants import (
    JUDGE_TOOL_MAX_CODE_BYTES,
    JUDGE_TOOL_POLL_INTERVAL_SEC,
    JUDGE_TOOL_WAIT_TIMEOUT_SEC,
)

logger = logging.getLogger(__name__)

# Actual outputs of hidden tests are only handed back to the model in a
# heavily truncated form, and the system prompt forbids reproducing them.
SNIPPET_LIMIT = 800

_TOOL_STATUS_LABELS = {
    'Accepted': '通过',
    'Wrong Answer': '答案错误',
    'Time Limit Exceeded': '超时',
    'Memory Limit Exceeded': '超内存',
    'Runtime Error': '运行错误',
    'Compile Error': '编译错误',
    'System Error': '评测系统错误',
}


def _bot_username() -> str:
    return getattr(settings, 'AI_JUDGE_BOT_USERNAME', '__ai_judge_bot__')


def get_ai_judge_user() -> User:
    """Return (creating on first use) the dedicated AI judge account.

    The account is created deactivated (``is_active=False``): it is a pure
    service identity that must never be able to log in, while still being a
    valid FK target for its judge submissions.
    """
    username = _bot_username()
    nickname = getattr(settings, 'AI_JUDGE_BOT_NICKNAME', 'AI 判题助手')
    user = User.objects.filter(username=username).first()
    if user is not None:
        if user.is_active:
            # Self-heal accounts created before login was banned.
            user.is_active = False
            user.save(update_fields=['is_active'])
            logger.info('Disabled login for AI judge account: %s', username)
        return user

    # Race-safe creation: two web workers may notice the missing row at once.
    user = User(
        username=username,
        email=f'{username}@ai.internal.guwu-oj.local',
        nickname=nickname,
        is_active=False,
        is_staff=False,
        is_superuser=False,
    )
    user.set_unusable_password()
    try:
        user.save()
        logger.info('Created dedicated AI judge account: %s (login disabled)', username)
    except Exception:
        # Lost the get_or_create race — fetch the winner.
        existing = User.objects.filter(username=username).first()
        if existing is not None:
            return existing
        raise
    return user


def validate_arguments(arguments: dict) -> tuple[str | None, str | None, str | None]:
    """Validate raw model-supplied tool arguments.

    Returns ``(language, code, error_message)``; ``error_message`` is set when
    the call is invalid.
    """
    if not isinstance(arguments, dict):
        return None, None, '工具参数必须是 JSON 对象。'
    language = (arguments.get('language') or '').strip()
    code = arguments.get('code')
    if not isinstance(code, str):
        return language or None, None, 'code 必须是字符串形式的完整源代码。'
    code = code.replace('\r\n', '\n')
    if language not in JUDGED_LANGUAGES:
        return language or None, code, (
            f'不支持的语言 "{language}"，可选：{sorted(JUDGED_LANGUAGES)}。'
        )
    if not code.strip():
        return language, code, '代码不能为空，请提交完整可运行的程序。'
    if len(code.encode('utf-8')) > JUDGE_TOOL_MAX_CODE_BYTES:
        return language, code, (
            f'代码过大（上限 {JUDGE_TOOL_MAX_CODE_BYTES // 1024} KB），请精简后重试。'
        )
    return language, code, None


def _snippet(text: str | None) -> str:
    if not text:
        return ''
    text = text.strip()
    if len(text) <= SNIPPET_LIMIT:
        return text
    return text[:SNIPPET_LIMIT] + '\n... (已截断)'


def _build_result_payload(
    *,
    language: str,
    submission: Submission | None,
    status: str,
    problem,
    error: str = '',
    judged: bool = True,
) -> dict:
    """Assemble the JSON-serialisable verdict given back to the model.

    ``ok`` means "the program was judged and passed every test". A timed-out
    wait, infrastructure error or non-Accepted verdict all yield ``ok=False``.
    """
    payload: dict = {
        'ok': False,
        'judged': judged,
        'submission_id': submission.id if submission else None,
        'language': language,
        'status': status,
        'status_label': _TOOL_STATUS_LABELS.get(status, status),
        'passed_cases': 0,
        'total_cases': 0,
        'runtime_ms': None,
        'memory_kb': None,
        'time_limit_ms': problem.time_limit,
        'memory_limit_mb': problem.memory_limit,
        'compile_error': '',
        'first_failure': None,
        'cases': [],
        'error': error,
    }
    if submission is None or not judged:
        return payload

    payload['runtime_ms'] = submission.runtime
    payload['memory_kb'] = submission.memory

    results = list(
        SubmissionTestResult.objects
        .filter(submission=submission)
        .order_by('case_index')
    )
    payload['total_cases'] = len(results)
    payload['passed_cases'] = sum(1 for r in results if r.status == 'Accepted')
    payload['cases'] = [
        {'case_index': r.case_index, 'status': r.status, 'runtime_ms': r.runtime}
        for r in results
    ]
    payload['ok'] = status == 'Accepted'

    failed = next((r for r in results if r.status not in ('Accepted', 'Skipped')), None)
    if status == 'Compile Error':
        ce = next((r for r in results if r.error_message), None)
        payload['compile_error'] = _snippet(ce.error_message if ce else '')
    elif failed is not None:
        payload['first_failure'] = {
            'case_index': failed.case_index,
            'status': failed.status,
            'runtime_ms': failed.runtime,
            'actual_output': _snippet(failed.actual_output),
            'error_message': _snippet(failed.error_message),
        }
    return payload


def _wait_for_verdict(submission: Submission, timeout: float, poll_interval: float) -> bool:
    """Block until the RQ worker finishes judging. ``False`` on timeout."""
    deadline = time.monotonic() + timeout
    while True:
        submission.refresh_from_db(fields=['status'])
        if submission.status != 'Pending':
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)


def submit_for_ai_judge(problem, language: str, code: str) -> dict:
    """Create a Submission under the AI service account, enqueue judging and
    block until the verdict is ready (or the wait budget is exhausted).

    Never raises for judge-side / queue-side problems: failures are reported
    in the returned payload so the model can react (and the user still gets
    the explanation).
    """
    if not problem.test_cases.exists():
        return _build_result_payload(
            language=language, submission=None, status='System Error',
            problem=problem, judged=True,
            error='该题目尚未配置测试数据，无法进行评测验证。',
        )

    bot = get_ai_judge_user()
    submission = None
    try:
        # Autocommit here; on_commit therefore runs immediately. If a caller
        # ever wraps this in atomic(), the job is queued after commit so the
        # worker can never observe a missing row.
        with transaction.atomic():
            submission = Submission.objects.create(
                problem=problem,
                user=bot,
                code=code,
                language=language,
                status='Pending',
            )
            transaction.on_commit(lambda: enqueue_judge(submission.id))
    except Exception:
        logger.exception('AI judge tool: failed to enqueue submission')
        return _build_result_payload(
            language=language, submission=submission, status='System Error',
            problem=problem, judged=False,
            error='判题系统暂时不可用（提交失败），请跳过验证直接完成讲解。',
        )

    timeout = getattr(settings, 'AI_JUDGE_TOOL_TIMEOUT_SEC', JUDGE_TOOL_WAIT_TIMEOUT_SEC)
    poll_interval = getattr(
        settings, 'AI_JUDGE_TOOL_POLL_INTERVAL_SEC', JUDGE_TOOL_POLL_INTERVAL_SEC,
    )
    try:
        finished = _wait_for_verdict(submission, float(timeout), float(poll_interval))
    except Exception:
        logger.exception(
            'AI judge tool: error waiting for submission %s', submission.id,
        )
        return _build_result_payload(
            language=language, submission=submission, status='System Error',
            problem=problem, judged=False,
            error='等待判题结果时发生错误，请跳过验证直接完成讲解。',
        )

    if not finished:
        logger.warning(
            'AI judge tool: submission %s still pending after %.0fs',
            submission.id, timeout,
        )
        return _build_result_payload(
            language=language, submission=submission, status='Pending',
            problem=problem, judged=False,
            error=(
                f'判题排队/评测超过 {int(timeout)} 秒仍未完成，可能是评测机繁忙。'
                '请不要继续重复提交，基于已有信息完成讲解。'
            ),
        )

    submission.refresh_from_db(fields=['status', 'runtime', 'memory'])
    return _build_result_payload(
        language=language, submission=submission, status=submission.status,
        problem=problem, judged=True,
    )


def payload_to_tool_message(payload: dict) -> str:
    """Render the verdict payload as the tool-message content for the model."""
    return json.dumps(payload, ensure_ascii=False)
