import json
import logging
import queue
import threading

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import (
    HttpResponse,
    HttpResponseRedirect,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.core.serializers.json import DjangoJSONEncoder

from problems.markdown_utils import render_markdown
from problems.models import Problem
from users.sliding_window import sliding_allow

from . import billing, judge_tool
from .constants import (
    GENERATE_RATE_LIMIT_PER_MINUTE,
    INTERVAL_CHOICES,
    INTERVAL_LABELS,
    INTERVAL_MONTH,
    INTERVAL_YEAR,
    MAX_GENERATIONS_PER_SESSION,
    MAX_JUDGE_TOOL_CALLS,
    PAID_PLANS,
    PLAN_DESCRIPTIONS,
    PLAN_LABELS,
    PLAN_PLUS,
    PLAN_PRO,
    PRICE_FEN,
    PRICE_YUAN,
)
from .deepseek_api import DeepSeekError, build_user_prompt, stream_answer
from .models import AIGeneration, AISession, AIToolCall, Subscription
from .quota import get_quota_status, resolve_plan

logger = logging.getLogger(__name__)

# Send an SSE comment frame this often when the upstream is silent, so
# idle connections survive Cloudflare (~100s) / nginx read timeouts.
SSE_HEARTBEAT_INTERVAL = 15.0

_WINDOW_LABELS = {'day': '每日', 'week': '每周', 'month': '每月'}
_WINDOW_RESETS_HUMAN = {'day': '今日 24:00', 'week': '下周一 00:00', 'month': '下月 1 日'}


def _quota_context(user) -> dict:
    status = get_quota_status(user)
    data = status.as_dict()
    data['window_labels'] = _WINDOW_LABELS
    data['window_resets_human'] = _WINDOW_RESETS_HUMAN
    # Pre-built rows for template rendering (no dict-index filter needed).
    display = []
    for window in ('day', 'week', 'month'):
        limit = data['limits'][window]
        if limit is None:
            continue
        used = data['used'][window]
        # Cap at 100 so the bar never overflows visually.
        percent = min(100, int(round(100 - (used / limit * 100)))) if limit else 0
        display.append({
            'window': window,
            'label': _WINDOW_LABELS[window],
            'used': used,
            'limit': limit,
            'remaining': max(0, limit - used),
            'percent': percent,
        })
    data['display'] = display
    return data


def _sse(event: dict) -> str:
    """Encode a single Server-Sent Events ``data:`` frame."""
    return 'data: ' + json.dumps(
        event,
        ensure_ascii=False,
        separators=(',', ':'),
        cls=DjangoJSONEncoder,   # handles datetime / date / Decimal / UUID / ...
    ) + '\n\n'


# ---------------------------------------------------------------------------
# AI interaction (intentionally limited to 1 answer + at most 1 regeneration)
# ---------------------------------------------------------------------------
@login_required
@never_cache
def ask(request, problem_id):
    problem = get_object_or_404(Problem, id=problem_id, is_public=True)
    session = (
        AISession.objects.filter(user=request.user, problem=problem)
        .select_related('problem')
        .order_by('-created_at')
        .first()
    )
    generations = []
    if session is not None:
        generations = list(
            session.generations.filter(success=True).prefetch_related('tool_calls')
        )

    quota = _quota_context(request.user)
    plan, subscription = resolve_plan(request.user)
    gen_count = len(generations)
    is_satisfied = bool(session and session.status == AISession.Status.SATISFIED)
    ai_state = {
        'problem_id': problem.id,
        'session_id': session.id if session else None,
        'gen_count': gen_count,
        'max_rounds': MAX_GENERATIONS_PER_SESSION,
        'satisfied': is_satisfied,
    }
    return render(request, 'ai_assistant/ask.html', {
        'problem': problem,
        'session': session,
        'generations': generations,
        'gen_count': gen_count,
        'is_satisfied': is_satisfied,
        'ai_state': ai_state,
        'quota': quota,
        'subscription': subscription,
        'plan_code': plan,
        'max_rounds': MAX_GENERATIONS_PER_SESSION,
        'max_judge_calls': MAX_JUDGE_TOOL_CALLS,
    })


def _json_error(message, status=400, code='error', extra=None):
    payload = {'ok': False, 'code': code, 'message': message}
    if extra:
        payload.update(extra)
    return JsonResponse(payload, status=status)


def _make_tool_executor(problem, generation):
    """Build the ``submit_to_judge`` executor for one generation.

    The DeepSeek tool loop calls this on a worker thread. It enforces the
    per-generation cap, records an :class:`AIToolCall` audit row for every
    real invocation and delegates the submission/wait to
    :mod:`ai_assistant.judge_tool`.
    """

    def execute(tool_name: str, arguments: dict, seq: int) -> dict:
        if tool_name != 'submit_to_judge':
            return {
                'ok': False, 'judged': False, 'status': 'Error',
                'language': (arguments or {}).get('language', ''),
                'error': f'未知工具 {tool_name}，本次只支持 submit_to_judge。',
            }

        if seq > MAX_JUDGE_TOOL_CALLS:
            logger.debug(
                'AI judge tool cap reached (gen=%s, seq=%s)', generation.id, seq,
            )
            return {
                'ok': False, 'judged': False, 'status': 'Error',
                'language': (arguments or {}).get('language', ''),
                'error': (
                    f'本次讲解的判题验证已达上限（{MAX_JUDGE_TOOL_CALLS} 次），'
                    '请基于已有验证结果直接给出最终讲解。'
                ),
            }

        language, code, invalid = judge_tool.validate_arguments(arguments or {})
        tool_call = AIToolCall.objects.create(
            generation=generation,
            seq=seq,
            tool_name=tool_name,
            language=language or '',
            code=code or '',
            status=AIToolCall.Status.PENDING,
        )

        if invalid:
            tool_call.status = AIToolCall.Status.INVALID
            tool_call.error_message = invalid[:300]
            tool_call.save(update_fields=['status', 'error_message'])
            return {
                'ok': False, 'judged': False, 'status': 'Invalid',
                'status_label': '参数无效',
                'language': language or '',
                'error': f'参数无效：{invalid}',
            }

        payload = judge_tool.submit_for_ai_judge(problem, language, code)
        tool_call.submission_id = payload.get('submission_id')
        raw_status = payload.get('status') or AIToolCall.Status.ERROR
        # A wait timeout is reported as status='Pending' + judged=False;
        # record it as a distinct terminal state on the audit row.
        if not payload.get('judged') and raw_status == AIToolCall.Status.PENDING:
            tool_call.status = AIToolCall.Status.TIMEOUT
        else:
            tool_call.status = raw_status
        tool_call.passed_cases = payload.get('passed_cases') or 0
        tool_call.total_cases = payload.get('total_cases') or 0
        tool_call.runtime_ms = payload.get('runtime_ms')
        tool_call.memory_kb = payload.get('memory_kb')
        tool_call.error_message = (payload.get('error') or '')[:300]
        tool_call.result_json = judge_tool.payload_to_tool_message(payload)
        # Guard against upstream verdict strings not covered by the choices.
        valid_statuses = {choice[0] for choice in AIToolCall.Status.choices}
        if tool_call.status not in valid_statuses:
            tool_call.status = AIToolCall.Status.ERROR
        tool_call.save()
        return payload

    return execute


@login_required
@require_POST
@never_cache
def generate(request, problem_id):
    """Stream a DeepSeek answer for ``problem`` as Server-Sent Events.

    Pre-flight failures (rate limit, round limit, quota) are returned as a
    plain JSON 4xx response *before* the stream opens, so the client can
    handle them with regular ``fetch`` logic. Once streaming starts, the
    body is ``text/event-stream`` with frames:

    * ``{"type": "start", "session_id", "round", "regenerate"}``
    * ``{"type": "delta", "text"}``           — zero or more, in order
    * ``{"type": "done",  "answer_html", ...}`` — exactly one, on success
    * ``{"type": "error", "code", "message"}`` — terminal on failure

    Quota is charged when a new explanation **starts** (``success=True`` at
    creation). If the AI service fails before producing any content, the
    charge is refunded (``success=False``). A mid-stream disconnect after
    content was received keeps the charge and saves the partial answer.
    Interrupted answers can be **continued** without additional charge by
    passing ``continue_generation_id``.
    """
    problem = get_object_or_404(Problem, id=problem_id, is_public=True)
    user = request.user

    # Anti-abuse: sliding window independent of plan quotas.
    rl_key = f'sl:ai:gen:user:{user.id}'
    if not sliding_allow(rl_key, GENERATE_RATE_LIMIT_PER_MINUTE, 60):
        return _json_error('操作过于频繁，请稍后再试。', status=429, code='rate_limited')

    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except (UnicodeDecodeError, json.JSONDecodeError):
        body = {}
    session_id = body.get('session_id')
    continue_generation_id = body.get('continue_generation_id')

    # --- Continue an interrupted generation (no charge) ------------------
    if continue_generation_id:
        generation = get_object_or_404(
            AIGeneration, id=continue_generation_id, user=user,
            problem=problem, success=True,
        )
        if not generation.error_message:
            return _json_error('该讲解未中断，无需继续。', status=400, code='not_interrupted')
        session = generation.session
        if session.status == AISession.Status.SATISFIED:
            return _json_error('本次互动已结束。', status=403, code='session_closed')
        is_continuation = True
        round_no = generation.round_no
        regenerate = round_no > 1
        continue_from = generation.answer or ''
    else:
        # --- New generation (charge quota at creation) --------------------
        is_continuation = False
        continue_from = ''

        # Resume the current interaction, or start a fresh one once the
        # previous session has been marked satisfied.
        session = None
        if session_id:
            session = AISession.objects.filter(
                id=session_id, user=user, problem=problem,
            ).first()
            if session is not None and session.status == AISession.Status.SATISFIED:
                session = None
        if session is None:
            session = (
                AISession.objects.filter(user=user, problem=problem, status=AISession.Status.ACTIVE)
                .order_by('-created_at')
                .first()
            )
            if session is None:
                session = AISession.objects.create(user=user, problem=problem)

        used_rounds = session.successful_generation_count
        if used_rounds >= MAX_GENERATIONS_PER_SESSION:
            return _json_error(
                '本次互动已达上限（初始回答 + 1 次重新生成）。',
                status=403, code='round_limit',
            )

        quota_status = get_quota_status(user)
        if not quota_status.allowed:
            return _json_error(
                '本周期的 AI 解题次数已用完，升级会员可获得更多次数。',
                status=403, code='quota_exceeded',
                extra={'quota': _quota_context(user)},
            )

        regenerate = used_rounds >= 1
        prompt = build_user_prompt(problem, regenerate=regenerate)
        round_no = used_rounds + 1

        # Charge quota immediately at creation. If the AI fails before
        # producing any content, ``_persist_failure`` will refund by
        # setting ``success=False``.
        generation = AIGeneration.objects.create(
            session=session, user=user, problem=problem, round_no=round_no,
            prompt=prompt, success=True,
        )

    def event_stream():
        parts: list[str] = []
        reasoning_parts: list[str] = []
        prompt_tokens = 0
        completion_tokens = 0
        finalised = False
        received_content = False

        def _persist_failure(message: str) -> None:
            nonlocal finalised
            if not is_continuation:
                # Refund: the AI didn't produce usable content for a new
                # generation. Continuations never change ``success`` (the
                # original charge stands).
                generation.success = False
            generation.error_message = message[:300]
            generation.save()
            finalised = True

        def _persist_success(answer: str, reasoning: str) -> None:
            nonlocal finalised
            if is_continuation:
                # Append the continuation to the existing partial answer.
                generation.answer = (generation.answer or '') + answer
                generation.reasoning = (generation.reasoning or '') + reasoning
                generation.error_message = ''
            else:
                generation.reasoning = reasoning
                generation.answer = answer
                generation.success = True
                generation.prompt_tokens = prompt_tokens
                generation.completion_tokens = completion_tokens
                generation.error_message = ''
            generation.save()
            finalised = True

        # Pump the (blocking) upstream iterator on a worker thread so the
        # response generator can emit SSE heartbeats while waiting. Without
        # this, a long reasoning phase (deepseek-flash can "think" for a
        # minute before the first content token) sends zero bytes to the
        # browser and intermediaries kill the idle connection (Cloudflare
        # ~100s -> 524, proxies -> 504), which users see as the page being
        # stuck on "AI 正在作答".
        events_q: queue.Queue = queue.Queue()
        stream_holder: dict = {}
        upstream = stream_answer(
            problem,
            regenerate=regenerate,
            continue_from=continue_from,
            stream_holder=stream_holder,
            tool_executor=_make_tool_executor(problem, generation),
        )

        def _pump() -> None:
            try:
                for event in upstream:
                    events_q.put(('event', event))
            except Exception as exc:  # propagated to the response generator
                events_q.put(('error', exc))
            finally:
                events_q.put(('end', None))
                # The executor opens its own DB connection on this thread
                # (AIToolCall rows / judge polling); release it when done.
                try:
                    from django.db import connection
                    connection.close()
                except Exception:  # pragma: no cover - best effort
                    logger.debug('Failed to close pump DB connection', exc_info=True)

        worker = threading.Thread(target=_pump, daemon=True)
        worker.start()
        received_done = False

        try:
            yield _sse({
                'type': 'start',
                'session_id': session.id,
                'generation_id': generation.id,
                'round': round_no,
                'regenerate': regenerate,
                'continue': is_continuation,
                'max_rounds': MAX_GENERATIONS_PER_SESSION,
                'max_judge_calls': MAX_JUDGE_TOOL_CALLS,
            })

            while True:
                try:
                    kind, payload = events_q.get(timeout=SSE_HEARTBEAT_INTERVAL)
                except queue.Empty:
                    # SSE comment frame: fetch/EventSource parsers ignore it,
                    # but it carries bytes through every proxy on the path.
                    yield ': ping\n\n'
                    continue

                if kind == 'error':
                    raise payload
                if kind == 'end':
                    break

                event = payload
                if event['type'] == 'reasoning':
                    received_content = True
                    reasoning_parts.append(event['text'])
                    yield _sse({'type': 'reasoning', 'text': event['text']})
                elif event['type'] == 'delta':
                    received_content = True
                    parts.append(event['text'])
                    yield _sse({'type': 'delta', 'text': event['text']})
                elif event['type'] == 'tool_call':
                    received_content = True
                    # Any answer fragments streamed on this turn belonged to
                    # the tool-call message, not to the final explanation.
                    parts.clear()
                    yield _sse(event)
                elif event['type'] == 'tool_result':
                    yield _sse(event)
                elif event['type'] == 'done':
                    received_done = True
                    prompt_tokens = event['prompt_tokens']
                    completion_tokens = event['completion_tokens']

            if not received_done:
                raise DeepSeekError('AI 服务中断，请重新生成一次。')

            answer = ''.join(parts)
            if not answer.strip():
                msg = 'AI 返回了空内容，请重新生成一次。'
                _persist_failure(msg)
                yield _sse({'type': 'error', 'code': 'empty', 'message': msg})
                return

            _persist_success(answer, ''.join(reasoning_parts))
            new_count = session.successful_generation_count
            yield _sse({
                'type': 'done',
                'ok': True,
                'session_id': session.id,
                'generation_id': generation.id,
                'round': new_count,
                'answer_html': render_markdown(
                    generation.answer if is_continuation else answer
                ),
                'can_regenerate': new_count < MAX_GENERATIONS_PER_SESSION,
                'regenerate': regenerate,
                'quota': _quota_context(user),
            })
        except DeepSeekError as exc:
            _persist_failure(exc.user_message)
            yield _sse({
                'type': 'error',
                'code': 'upstream_error',
                'message': exc.user_message,
            })
            return
        except Exception:  # pragma: no cover - defensive
            logger.exception('AI streaming failed')
            msg = 'AI 服务暂时不可用，请稍后再试。'
            _persist_failure(msg)
            yield _sse({'type': 'error', 'code': 'internal', 'message': msg})
            return
        except GeneratorExit:
            # Client closed the connection before the answer was finalised.
            # Shut the raw upstream HTTP stream — the pump thread is blocked
            # inside it, so closing the generator itself is not possible
            # cross-thread ("generator already executing").
            raw_stream = stream_holder.get('stream')
            if raw_stream is not None:
                try:
                    raw_stream.close()
                except Exception:  # pragma: no cover - best effort
                    logger.debug('Failed to close upstream AI stream', exc_info=True)
            if not finalised:
                if is_continuation:
                    # Append whatever was received to the existing answer.
                    generation.answer = (generation.answer or '') + ''.join(parts)
                    generation.reasoning = (generation.reasoning or '') + ''.join(reasoning_parts)
                    generation.error_message = '连接中断，已保存部分内容。'[:300]
                    generation.save()
                elif received_content:
                    # New generation with content: keep the charge, save
                    # the partial answer so the user can continue later.
                    generation.reasoning = ''.join(reasoning_parts)
                    generation.answer = ''.join(parts)
                    generation.error_message = '连接中断，已保存部分内容。'[:300]
                    generation.save()
                else:
                    # New generation without any content: refund the charge.
                    generation.success = False
                    generation.error_message = '连接中断，未完成作答。'[:300]
                    generation.save()
            raise

    response = StreamingHttpResponse(
        event_stream(),
        content_type='text/event-stream; charset=utf-8',
    )
    # ``@never_cache`` already sets Cache-Control; this is the nginx-specific
    # hint that stops the proxy from buffering the whole body before sending.
    response['X-Accel-Buffering'] = 'no'
    return response


@login_required
@require_POST
@never_cache
def satisfied(request, session_id):
    session = get_object_or_404(AISession, id=session_id, user=request.user)
    if session.status != AISession.Status.SATISFIED:
        session.status = AISession.Status.SATISFIED
        session.save(update_fields=['status', 'updated_at'])
    return JsonResponse({'ok': True})


# ---------------------------------------------------------------------------
# Plans / billing
# ---------------------------------------------------------------------------
def _plan_cards():
    cards = []
    for plan in (PLAN_PLUS, PLAN_PRO):
        yearly = PRICE_YUAN[(plan, INTERVAL_YEAR)]
        cards.append({
            'plan': plan,
            'label': PLAN_LABELS[plan],
            'description': PLAN_DESCRIPTIONS[plan],
            'monthly_yuan': PRICE_YUAN[(plan, INTERVAL_MONTH)],
            'yearly_yuan': yearly,
            'yearly_monthly_yuan': round(yearly / 12, 2),
        })
    return cards


@never_cache
def pricing(request):
    subscription = None
    quota = None
    if request.user.is_authenticated:
        _, subscription = resolve_plan(request.user)
        quota = _quota_context(request.user)
    return render(request, 'ai_assistant/pricing.html', {
        'cards': _plan_cards(),
        'interval_choices': INTERVAL_CHOICES,
        'interval_labels': INTERVAL_LABELS,
        'plan_labels': PLAN_LABELS,
        'subscription': subscription,
        'quota': quota,
        'stripe_configured': bool(settings.STRIPE_SECRET_KEY),
    })


@login_required
@require_POST
def checkout(request):
    plan = request.POST.get('plan', '').strip()
    interval = request.POST.get('interval', '').strip()
    if plan not in PAID_PLANS or interval not in (INTERVAL_MONTH, INTERVAL_YEAR):
        messages.error(request, '请选择有效的套餐与计费周期。')
        return redirect('ai_assistant:pricing')
    try:
        session = billing.create_checkout_session(request, request.user, plan, interval)
    except billing.BillingError as exc:
        messages.error(request, str(exc))
        return redirect('ai_assistant:pricing')
    except Exception as exc:  # stripe errors / network
        logger.exception('Stripe checkout failed: %s', exc)
        messages.error(request, '创建支付会话失败，请稍后再试。')
        return redirect('ai_assistant:pricing')
    return HttpResponseRedirect(session.url)


@login_required
@never_cache
def billing_success(request):
    session_id = request.GET.get('session_id', '').strip()
    synced = None
    if session_id:
        try:
            synced = billing.sync_checkout_session(session_id, expected_user=request.user)
        except Exception as exc:
            logger.exception('Post-checkout sync failed: %s', exc)
    return render(request, 'ai_assistant/result.html', {
        'synced': synced is not None,
        'pricing_url': reverse('ai_assistant:pricing'),
    })


@login_required
@require_POST
def cancel_subscription(request):
    sub = Subscription.objects.filter(user=request.user).first()
    if sub is None or not sub.is_paid_active or not sub.stripe_subscription_id:
        messages.error(request, '当前没有可取消的生效订阅。')
        return redirect('ai_assistant:pricing')
    try:
        billing.cancel_at_period_end(sub)
    except Exception as exc:
        logger.exception('Stripe cancel failed: %s', exc)
        messages.error(request, '取消订阅失败，请稍后再试或联系管理员。')
    else:
        messages.success(
            request,
            '已安排在当前计费周期结束后取消，到期前你仍可使用会员权益。',
        )
    return redirect('ai_assistant:pricing')


@csrf_exempt
@require_POST
def stripe_webhook(request):
    sig = request.META.get('HTTP_STRIPE_SIGNATURE', '')
    try:
        event = billing.parse_webhook_event(request.body, sig)
        billing.handle_event(event)
    except Exception as exc:
        logger.warning('Stripe webhook rejected: %s', exc)
        return HttpResponse('invalid', status=400)
    return HttpResponse('ok', status=200)
