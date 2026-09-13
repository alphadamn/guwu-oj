"""Complete missing problem tags via DeepSeek, using a Chinese tag vocabulary.

Tags are a CharField. Import scripts store provenance keys (``cc:`` / ``cf:`` /
洛谷题号) plus optional algorithm labels. Many imported problems only have the
provenance half; this module fills Chinese algorithm tags.
"""
from __future__ import annotations

import json
import re
from typing import Iterable

from django.core.cache import cache
from django.utils.html import strip_tags

from .models import Problem, split_stored_tags
from .tag_labels import (
    CANONICAL_ZH_TAGS,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_USER_PROMPT,
    PROMPT_CACHE_KEY,
    PROMPT_MAX_LEN,
    has_cjk,
    is_provenance_tag,
    to_zh_algorithm_tag,
)

TAGS_MAX_LEN = 200
MAX_BATCH = 100
STATEMENT_CHARS = 3500
VOCAB_MAX = 160


def algorithm_tags(tags: Iterable[str]) -> list[str]:
    return [t for t in tags if not is_provenance_tag(t)]


def needs_algorithm_tags(problem: Problem) -> bool:
    return not algorithm_tags(split_stored_tags(problem.tags))


def incomplete_tag_queryset():
    """Problems whose stored tags have no algorithm label yet."""
    return Problem.objects.all().order_by('id')


def incomplete_problems(*, limit: int | None = None):
    found = []
    qs = incomplete_tag_queryset().only(
        'id', 'title', 'tags', 'description',
        'input_format', 'output_format', 'hint',
    )
    for problem in qs.iterator():
        if needs_algorithm_tags(problem):
            found.append(problem)
            if limit is not None and len(found) >= limit:
                break
    return found


def count_incomplete_problems() -> int:
    n = 0
    for tags in Problem.objects.values_list('tags', flat=True).iterator():
        if not algorithm_tags(split_stored_tags(tags)):
            n += 1
    return n


def collect_vocabulary() -> list[str]:
    """Chinese algorithm tags: canonical list plus 中文标签 already in the DB."""
    extra_counts: dict[str, int] = {}
    for raw in Problem.objects.values_list('tags', flat=True).iterator():
        for tag in algorithm_tags(split_stored_tags(raw)):
            zh = to_zh_algorithm_tag(tag)
            if zh and (has_cjk(zh) or zh in CANONICAL_ZH_TAGS):
                extra_counts[zh] = extra_counts.get(zh, 0) + 1
    ordered = list(CANONICAL_ZH_TAGS)
    seen = set(ordered)
    for tag, _n in sorted(extra_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if tag not in seen:
            ordered.append(tag)
            seen.add(tag)
        if len(ordered) >= VOCAB_MAX:
            break
    return ordered[:VOCAB_MAX]


def load_saved_prompts() -> dict[str, str]:
    saved = cache.get(PROMPT_CACHE_KEY) or {}
    system = (saved.get('system') or DEFAULT_SYSTEM_PROMPT).strip()
    user = (saved.get('user') or DEFAULT_USER_PROMPT).strip()
    return {
        'system': system or DEFAULT_SYSTEM_PROMPT,
        'user': user or DEFAULT_USER_PROMPT,
    }


def save_prompts(system: str, user: str) -> dict[str, str]:
    system = (system or '').strip()[:PROMPT_MAX_LEN] or DEFAULT_SYSTEM_PROMPT
    user = (user or '').strip()[:PROMPT_MAX_LEN] or DEFAULT_USER_PROMPT
    payload = {'system': system, 'user': user}
    cache.set(PROMPT_CACHE_KEY, payload, None)
    return payload


def join_tags(tags: Iterable[str]) -> str:
    seen = set()
    ordered = []
    for tag in tags:
        tag = (tag or '').strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        ordered.append(tag)
    joined = ','.join(ordered)
    while ordered and len(joined) > TAGS_MAX_LEN:
        ordered.pop()
        joined = ','.join(ordered)
    return joined[:TAGS_MAX_LEN]


def _statement_excerpt(problem: Problem) -> str:
    chunks = [
        strip_tags(problem.description or ''),
        strip_tags(problem.input_format or ''),
        strip_tags(problem.output_format or ''),
        strip_tags(problem.hint or ''),
    ]
    text = '\n'.join(part.strip() for part in chunks if part and part.strip())
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    if len(text) > STATEMENT_CHARS:
        text = text[:STATEMENT_CHARS] + '\n…'
    return text


def render_user_prompt(template: str, problem: Problem, vocab: list[str]) -> str:
    excerpt = _statement_excerpt(problem)
    try:
        return template.format(
            id=problem.id,
            title=problem.title or '',
            difficulty=problem.difficulty or '',
            vocab=', '.join(vocab),
            statement=excerpt,
        )
    except (KeyError, IndexError, ValueError):
        return (
            f'题号：P{problem.id}\n标题：{problem.title}\n'
            f'难度：{problem.difficulty or ""}\n'
            f'可选中文标签词表：\n' + ', '.join(vocab) + '\n\n'
            f'题面：\n{excerpt}'
        )


def _parse_tag_list(raw: str, vocab: set[str]) -> list[str]:
    raw = (raw or '').strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(data, dict):
        tags = data.get('tags', data.get('tag', []))
    elif isinstance(data, list):
        tags = data
    else:
        return []
    if isinstance(tags, str):
        tags = split_stored_tags(tags)
    chosen = []
    canonical = set(CANONICAL_ZH_TAGS)
    for item in tags or []:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if name not in vocab or name in chosen:
            continue
        if not has_cjk(name) and name not in canonical:
            continue
        chosen.append(name)
    return chosen[:8]


def suggest_tags_for_problem(
    problem: Problem,
    vocab: list[str],
    *,
    api_key: str,
    system_prompt: str | None = None,
    user_prompt_template: str | None = None,
) -> list[str]:
    from ai_assistant.deepseek_api import chat_json

    vocab_set = set(vocab)
    system = (system_prompt or DEFAULT_SYSTEM_PROMPT).strip() or DEFAULT_SYSTEM_PROMPT
    # DeepSeek json_object mode requires the literal word "json" in the prompt.
    if 'json' not in system.lower():
        system = (
            system.rstrip()
            + '\n只输出 json 对象：{"tags": ["标签1", "标签2"]}。'
        )
    template = (user_prompt_template or DEFAULT_USER_PROMPT).strip() or DEFAULT_USER_PROMPT
    user = render_user_prompt(template, problem, vocab)
    data = chat_json(
        api_key=api_key,
        messages=[
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ],
        temperature=0.2,
        max_tokens=400,
        base_url=official_deepseek_base_url(),
        timeout=60,
    )
    if isinstance(data, dict):
        raw = json.dumps(data, ensure_ascii=False)
    else:
        raw = str(data)
    return _parse_tag_list(raw, vocab_set)


def apply_suggested_tags(problem: Problem, suggested: list[str]) -> str:
    existing = split_stored_tags(problem.tags)
    merged = join_tags(existing + suggested)
    return merged


def complete_one_problem(
    problem: Problem,
    vocab: list[str],
    *,
    api_key: str,
    system_prompt: str | None = None,
    user_prompt_template: str | None = None,
) -> dict:
    """Suggest and persist Chinese algorithm tags. Returns a JSON-safe result dict."""
    before = problem.tags or ''
    suggested = suggest_tags_for_problem(
        problem, vocab, api_key=api_key,
        system_prompt=system_prompt,
        user_prompt_template=user_prompt_template,
    )
    suggested = [
        t for t in suggested
        if has_cjk(t) or t in set(CANONICAL_ZH_TAGS)
    ]
    if not suggested:
        return {
            'ok': False,
            'problem_id': problem.id,
            'title': problem.title,
            'error': '模型未返回词表内中文标签',
            'before': before,
            'after': before,
            'added': [],
        }
    after = apply_suggested_tags(problem, suggested)
    if after == before:
        return {
            'ok': False,
            'problem_id': problem.id,
            'title': problem.title,
            'error': '标签未变化',
            'before': before,
            'after': after,
            'added': suggested,
        }
    problem.tags = after
    problem.save(update_fields=['tags', 'updated_at'])
    return {
        'ok': True,
        'problem_id': problem.id,
        'title': problem.title,
        'before': before,
        'after': after,
        'added': suggested,
    }


def official_deepseek_base_url() -> str:
    # Always the official product API for this admin tool, regardless of the
    # site-wide AI 助教 endpoint (which may point at a compatible proxy).
    return 'https://api.deepseek.com'
