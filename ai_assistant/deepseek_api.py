"""Thin wrapper around the DeepSeek chat API (OpenAI-compatible).

Streaming edition.

A failure here (network / upstream / insufficient balance) MUST NOT consume
the user's quota: callers persist a ``success=False`` row and return an
error to the UI without counting the generation.

Because the response is streamed, the caller can no longer rely on an
exception to know whether anything was produced. Accumulate the ``delta``
chunks and only persist ``success=True`` / charge quota once the final
``done`` event arrives.
"""
from __future__ import annotations

import json
import logging
from typing import Callable, Iterator

from django.conf import settings
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    RateLimitError,
)

from .constants import MAX_JUDGE_TOOL_CALLS

logger = logging.getLogger(__name__)


class DeepSeekError(Exception):
    """Generic upstream failure. ``user_message`` is safe to display."""

    def __init__(self, user_message: str = 'AI 服务暂时不可用，请稍后再试。'):
        super().__init__(user_message)
        self.user_message = user_message


class DeepSeekBalanceError(DeepSeekError):
    """Raised when the DeepSeek account is out of balance / quota."""


SYSTEM_PROMPT = (
    '你是「谷物 OJ」在线编程评测平台（OJ）的算法助教，服务对象是正在独立做题的选手。\n'
    '请严格遵守以下原则：\n'
    '1. 目标是启发与教学，帮助用户自己 AC，而不是替他交作业。不要给出可以直接提交、'
    '整题照搬的完整 AC 代码；可以给出关键代码片段、核心伪代码或局部实现。\n'
    '2. 使用简体中文，结构清晰，合理使用 Markdown 标题、列表与代码块。\n'
    '3. 一次回答依次包含：① 题意与关键条件提炼；② 解题思路与算法选择（说明为什么）；'
    '③ 关键步骤 / 状态设计 / 转移或贪心策略；④ 需要注意的边界与坑；'
    '⑤ 复杂度分析；⑥ 必要时给出核心代码片段或伪代码。\n'
    '4. 数学公式用 $...$ 或 $$...$$；只围绕本题作答，拒绝无关请求。\n'
    '5. 如果题目信息不足，明确指出需要澄清的点。'
)

# Appended to the system prompt only when the judge tool is available.
TOOL_SYSTEM_SUFFIX = (
    '\n\n## 判题验证工具（submit_to_judge）\n'
    '你可以调用 submit_to_judge，把一份你自己编写的、完整可独立编译运行的参考程序'
    '提交到本题的真实判题系统进行评测，用来验证你的思路、算法实现与边界处理是否正确。'
    '该提交使用平台专用的 AI 服务账号，与提问用户的账号、提交记录和积分完全无关。\n'
    f'1. 每次讲解最多调用 {MAX_JUDGE_TOOL_CALLS} 次，请珍惜：优先用于验证核心算法、'
    '易错边界或你没有十足把握的实现；确有把握时可以直接讲解，不要为了调用而调用。\n'
    '2. 提交的必须是完整程序（含输入输出与主函数），语言从工具枚举中选择，'
    '严格遵守题目给定的输入输出格式与数据范围。\n'
    '3. 工具返回各测试点的判定、耗时，以及首个失败点的简短实际输出/报错片段'
    '（不返回完整期望输出）。这些隐藏测试数据仅供你调试思路，'
    '严禁在讲解中原样转述或泄露给用户，只应给出结论性的分析。\n'
    '4. 如果没有 AC，先根据判定结果定位并修正问题，必要时可再次验证；'
    '若返回评测机繁忙/超时，不要重复提交，直接基于已有信息完成讲解。\n'
    '5. 即使参考程序 AC，最终讲解仍必须遵守启发式教学原则：'
    '不要把整份可直接提交的 AC 代码贴给用户，只给关键片段或伪代码并讲清原理。'
)

SUBMIT_JUDGE_TOOL = {
    'type': 'function',
    'function': {
        'name': 'submit_to_judge',
        'description': (
            '将一份完整可运行的解题程序提交到本题的真实判题系统进行评测，'
            '验证思路与实现是否正确。使用专用 AI 服务账号提交，'
            '与用户账号无关。每次讲解最多调用 %d 次。'
        ) % MAX_JUDGE_TOOL_CALLS,
        'parameters': {
            'type': 'object',
            'properties': {
                'language': {
                    'type': 'string',
                    'enum': [
                        'C++', 'C', 'Python', 'Java', 'JavaScript',
                        'Golang', 'Rust', 'Ruby', 'Kotlin', 'Assembly',
                    ],
                    'description': '编程语言，必须与提交代码匹配。',
                },
                'code': {
                    'type': 'string',
                    'description': (
                        '完整的程序源代码（包含主函数、输入读取与输出），'
                        '可直接编译运行，而不是代码片段。'
                    ),
                },
            },
            'required': ['language', 'code'],
        },
    },
}

# Upper bound on request turns (tool calls + final answer). Three judge calls
# plus one final turn is the expected maximum; leave headroom for parallel /
# corrective turns without letting a misbehaving model loop forever.
MAX_TOOL_LOOP_TURNS = MAX_JUDGE_TOOL_CALLS * 2 + 2

# Signature of the executor supplied by the web layer:
#   executor(tool_name: str, arguments: dict, seq: int) -> dict
# The returned dict is handed back to the model as the tool message and also
# forwarded (pruned) to the browser.
ToolExecutor = Callable[[str, dict, int], dict]


def _client() -> OpenAI:
    return OpenAI(
        api_key=settings.DEEPSEEK_API_KEY,
        base_url=settings.DEEPSEEK_BASE_URL,
        timeout=settings.DEEPSEEK_TIMEOUT,
        max_retries=1,
    )


def build_user_prompt(problem, *, regenerate: bool = False) -> str:
    parts = [f'题号：P{problem.id}', f'题目标题：{problem.title}']
    if problem.difficulty:
        parts.append(f'难度：{problem.difficulty}')
    if problem.tags:
        parts.append(f'标签：{problem.tags}')
    parts.append(f'题目描述：\n{problem.description}')
    parts.append(f'输入格式：\n{problem.input_format}')
    parts.append(f'输出格式：\n{problem.output_format}')
    if problem.sample_input:
        parts.append(f'样例输入：\n{problem.sample_input}')
    if problem.sample_output:
        parts.append(f'样例输出：\n{problem.sample_output}')
    if problem.hint:
        parts.append(f'题目提示：\n{problem.hint}')

    if regenerate:
        parts.append(
            '\n我对刚才的讲解不满意。请换一个不同的切入角度（或更通俗、更详细地）'
            '重新讲解这道题，避免与上一版雷同，仍然遵循启发式原则。'
        )
    else:
        parts.append('\n请作为算法助教，按系统约定的结构给我讲解这道题。')
    return '\n\n'.join(parts)


def _build_messages(
    problem, *, regenerate: bool, with_judge_tool: bool = False,
) -> list[dict]:
    system_content = SYSTEM_PROMPT
    if with_judge_tool:
        system_content += TOOL_SYSTEM_SUFFIX
    return [
        {'role': 'system', 'content': system_content},
        {'role': 'user', 'content': build_user_prompt(problem, regenerate=regenerate)},
    ]


def _is_balance_error(exc: APIStatusError) -> bool:
    text = f'{getattr(exc, "message", "")} {getattr(exc, "code", "")}'.lower()
    markers = ('balance', 'insufficient', 'quota', '余额', '欠费', '充值')
    return any(m in text for m in markers)


def _to_deepseek_error(exc: Exception) -> DeepSeekError:
    """Map an upstream exception onto a user-safe ``DeepSeekError``.

    NOTE: ``RateLimitError`` must be checked *before* ``APIStatusError`` —
    in openai>=1.x it is a subclass, so the old ordering made the
    ``except RateLimitError`` branch dead code.
    """
    if isinstance(exc, RateLimitError):
        logger.warning('DeepSeek rate limited: %s', exc)
        return DeepSeekBalanceError('AI 服务当前繁忙，请稍后再试。')
    if isinstance(exc, APIStatusError):
        logger.warning('DeepSeek API status error: %s', exc)
        if getattr(exc, 'status_code', 0) in (402, 429) or _is_balance_error(exc):
            return DeepSeekBalanceError(
                'AI 答疑额度暂时不足，服务恢复后即可继续使用，请稍后再试。'
            )
        message = str(getattr(exc, 'message', '') or exc)
        if 'json_object' in message.lower() or "word 'json'" in message.lower():
            return DeepSeekError(
                '提示词里需要包含英文单词 json（DeepSeek JSON 模式要求）。'
                '请在系统提示词中写明只输出 json。'
            )
        if getattr(exc, 'status_code', 0) == 400:
            return DeepSeekError('DeepSeek 拒绝了请求，请检查 API Key 或提示词。')
        if getattr(exc, 'status_code', 0) == 401:
            return DeepSeekError('DeepSeek API Key 无效，请重新填写。')
        return DeepSeekError()
    if isinstance(exc, (APITimeoutError, APIConnectionError)):
        logger.warning('DeepSeek API connection error: %s', exc)
        return DeepSeekError('AI 服务连接超时，请检查网络后稍后再试。')
    logger.exception('DeepSeek unexpected error: %s', exc)
    return DeepSeekError()


def _new_tool_slot() -> dict:
    return {'id': '', 'name': '', 'arguments': ''}


def _accumulate_tool_calls(tool_acc: dict, tool_call_deltas) -> None:
    """Merge streamed ``message.tool_calls`` deltas into ``tool_acc``.

    Delta chunks carry per-index fragments (id once, then function name /
    arguments pieces), so each index is assembled independently.
    """
    for tcd in tool_call_deltas or []:
        idx = getattr(tcd, 'index', None)
        if idx is None:
            continue
        slot = tool_acc.setdefault(idx, _new_tool_slot())
        if getattr(tcd, 'id', None):
            slot['id'] = tcd.id
        fn = getattr(tcd, 'function', None)
        if fn is not None:
            if getattr(fn, 'name', None):
                slot['name'] += fn.name
            if getattr(fn, 'arguments', None):
                slot['arguments'] += fn.arguments


def _safe_parse_arguments(raw: str) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def stream_answer(
    problem,
    *,
    regenerate: bool = False,
    stream_holder: dict | None = None,
    tool_executor: ToolExecutor | None = None,
) -> Iterator[dict]:
    """Stream DeepSeek's reply for ``problem``, optionally with judge tools.

    Yields dicts:

    * ``{'type': 'reasoning', 'text': str}`` — zero or more chain-of-thought
      fragments, always before the answer fragments.
    * ``{'type': 'delta', 'text': str}`` — an incremental piece of the answer,
      in order. Concatenating every ``text`` yields the full Markdown answer.
      When the model calls a tool mid-stream the browser should discard any
      earlier answer fragments of the same turn (a ``tool_call`` event marks
      that boundary).
    * ``{'type': 'tool_call', 'seq', 'max', 'name', 'language'}`` — the model
      is about to invoke a tool (judging starts, may take tens of seconds).
    * ``{'type': 'tool_result', 'seq', ...verdict fields}`` — verdict ready.
    * ``{'type': 'done', 'prompt_tokens', 'completion_tokens'}`` — exactly
      one, as the last item, when the stream finished cleanly.

    When ``tool_executor`` is given, OpenAI-style function calling is enabled
    for ``submit_to_judge``. The executor performs the real submission and
    returns a JSON-serialisable verdict dict. It is also responsible for the
    per-generation cap (``seq`` starts at 1); an over-cap call must return an
    error payload instead of judging.

    ``stream_holder``, when given, receives the raw openai ``Stream`` under
    the ``'stream'`` key once the first upstream connection opens. The caller
    may then call ``stream.close()`` from another thread to abort a blocked
    read (e.g. on client disconnect).

    Raises ``DeepSeekError`` (never an openai exception) on failure.
    """
    if not settings.DEEPSEEK_API_KEY:
        raise DeepSeekError('AI 解题尚未配置 API Key，请联系管理员。')

    client = _client()
    extra = {}
    if getattr(settings, 'DEEPSEEK_STREAM_INCLUDE_USAGE', True):
        # DeepSeek supports OpenAI's stream_options; the final chunk then
        # carries ``usage`` (the chunk itself has no choices).
        extra['stream_options'] = {'include_usage': True}

    use_tools = tool_executor is not None
    messages = _build_messages(
        problem, regenerate=regenerate, with_judge_tool=use_tools,
    )
    tools = [SUBMIT_JUDGE_TOOL] if use_tools else None

    # Usage is reported per HTTP request; prompt tokens grow with history, so
    # take the max, while completion tokens of every turn are additive.
    prompt_tokens = 0
    completion_tokens = 0
    tool_seq = 0
    stream_registered = False

    for _turn in range(MAX_TOOL_LOOP_TURNS):
        try:
            stream = client.chat.completions.create(
                model=settings.DEEPSEEK_MODEL,
                messages=messages,
                temperature=0.9 if regenerate else 0.6,
                stream=True,
                max_tokens=128000,
                tools=tools,
                tool_choice='auto' if use_tools else None,
                **extra,
            )
        except Exception as exc:
            # 4xx/5xx and connection failures surface here, before any output.
            raise _to_deepseek_error(exc) from exc

        if stream_holder is not None and not stream_registered:
            stream_holder['stream'] = stream
            stream_registered = True

        content_parts: list[str] = []
        tool_acc: dict = {}
        finish_reason = None

        try:
            for chunk in stream:
                usage = getattr(chunk, 'usage', None)
                if usage is not None:
                    prompt_tokens = max(
                        prompt_tokens, getattr(usage, 'prompt_tokens', 0) or 0,
                    )
                    completion_tokens += getattr(
                        usage, 'completion_tokens', 0,
                    ) or 0

                choices = getattr(chunk, 'choices', None)
                if not choices:
                    continue  # usage-only chunk, or keep-alive
                choice = choices[0]
                if getattr(choice, 'finish_reason', None):
                    finish_reason = choice.finish_reason
                delta = getattr(choice, 'delta', None)
                content = getattr(delta, 'content', None)
                # DeepSeek's own API (deepseek-flash) streams the
                # chain-of-thought in ``reasoning_content``; OpenRouter-style
                # compatible endpoints use ``reasoning``. Check both names.
                reasoning = (
                    getattr(delta, 'reasoning_content', None)
                    or getattr(delta, 'reasoning', None)
                )
                if reasoning:
                    yield {'type': 'reasoning', 'text': reasoning}
                if content:
                    content_parts.append(content)
                    yield {'type': 'delta', 'text': content}
                _accumulate_tool_calls(
                    tool_acc, getattr(delta, 'tool_calls', None),
                )
        except Exception as exc:
            # Connection dropped / timeout in the middle of the body.
            raise _to_deepseek_error(exc) from exc

        if finish_reason != 'tool_calls' or not tool_acc or not use_tools:
            answer = ''.join(content_parts)
            if not answer.strip():
                # A turn ending without content and without a tool call is a
                # degenerate response — never persist it as a success.
                raise DeepSeekError('AI 返回了空内容，请重新生成一次。')
            yield {
                'type': 'done',
                'prompt_tokens': prompt_tokens,
                'completion_tokens': completion_tokens,
            }
            return

        # ---- Tool-call turn: echo the assistant message, execute, reply ----
        ordered_calls = [tool_acc[i] for i in sorted(tool_acc)]
        messages.append({
            'role': 'assistant',
            'content': ''.join(content_parts) or None,
            'tool_calls': [
                {
                    'id': call['id'] or f'call_{tool_seq + idx + 1}',
                    'type': 'function',
                    'function': {
                        'name': call['name'],
                        'arguments': call['arguments'],
                    },
                }
                for idx, call in enumerate(ordered_calls)
            ],
        })

        for idx, call in enumerate(ordered_calls):
            tool_seq += 1
            name = call['name'] or 'submit_to_judge'
            arguments = _safe_parse_arguments(call['arguments'])
            yield {
                'type': 'tool_call',
                'seq': tool_seq,
                'max': MAX_JUDGE_TOOL_CALLS,
                'name': name,
                'language': arguments.get('language', ''),
            }
            try:
                payload = tool_executor(name, arguments, tool_seq)
                if not isinstance(payload, dict):
                    payload = {'ok': False, 'judged': False,
                               'status': 'Error', 'error': '工具返回格式错误。'}
            except Exception:
                logger.exception('AI judge tool executor failed')
                payload = {
                    'ok': False, 'judged': False, 'status': 'Error',
                    'error': '判题工具执行异常，请跳过验证直接完成讲解。',
                }

            yield {
                'type': 'tool_result',
                'seq': tool_seq,
                'name': name,
                'ok': bool(payload.get('ok')),
                'judged': bool(payload.get('judged')),
                'status': payload.get('status', 'Error'),
                'status_label': payload.get('status_label', payload.get('status', '')),
                'language': payload.get('language', arguments.get('language', '')),
                'passed_cases': payload.get('passed_cases', 0),
                'total_cases': payload.get('total_cases', 0),
                'runtime_ms': payload.get('runtime_ms'),
                'memory_kb': payload.get('memory_kb'),
                'error': payload.get('error', ''),
            }
            messages.append({
                'role': 'tool',
                'tool_call_id': call['id'] or f'call_{tool_seq}',
                'name': name,
                'content': json.dumps(payload, ensure_ascii=False),
            })

    raise DeepSeekError('AI 工具调用次数超出限制，请重新生成一次。')


def _client_with_key(api_key: str, *, base_url: str | None = None, timeout: float | None = None) -> OpenAI:
    return OpenAI(
        api_key=api_key,
        base_url=(base_url or settings.DEEPSEEK_BASE_URL),
        timeout=timeout if timeout is not None else settings.DEEPSEEK_TIMEOUT,
        max_retries=1,
    )


def chat_json(
    *,
    api_key: str,
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 512,
    base_url: str | None = None,
    timeout: float | None = None,
) -> dict:
    """Non-streaming JSON-object chat completion (official OpenAI-compatible API).

    ``api_key`` is supplied by the caller (e.g. an admin's own key) and must
    never be logged.
    """
    if not api_key or not str(api_key).strip():
        raise DeepSeekError('未提供 DeepSeek API Key。')

    client = _client_with_key(
        str(api_key).strip(),
        base_url=base_url,
        timeout=timeout,
    )
    try:
        response = client.chat.completions.create(
            model=model or getattr(settings, 'DEEPSEEK_TAG_MODEL', None) or 'deepseek-chat',
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
            response_format={'type': 'json_object'},
        )
    except Exception as exc:
        raise _to_deepseek_error(exc) from exc

    choice = (response.choices or [None])[0]
    content = ''
    if choice is not None:
        message = getattr(choice, 'message', None)
        content = getattr(message, 'content', None) or ''
    if not str(content).strip():
        raise DeepSeekError('AI 返回了空内容，请重新生成一次。')
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        raise DeepSeekError('AI 返回的不是合法 JSON。') from None
    if not isinstance(parsed, dict):
        raise DeepSeekError('AI 返回的 JSON 不是对象。')
    return parsed


def generate_answer(problem, *, regenerate: bool = False) -> tuple[str, int, int]:
    """Blocking helper: drain ``stream_answer`` and return the whole answer.

    Useful for tests, management commands and any non-streaming code path.
    """
    parts: list[str] = []
    prompt_tokens = completion_tokens = 0
    for event in stream_answer(problem, regenerate=regenerate):
        if event['type'] == 'delta':
            parts.append(event['text'])
        elif event['type'] == 'done':
            prompt_tokens = event['prompt_tokens']
            completion_tokens = event['completion_tokens']
    return ''.join(parts), prompt_tokens, completion_tokens
