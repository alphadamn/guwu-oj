#!/usr/bin/env python
"""Supplement test cases for problems with fewer than 3 test cases.

For every problem whose test case count is below ``--min-tests`` (default 3),
call the DeepSeek chat API to generate additional (input, expected_output)
pairs that follow the problem's I/O format, then persist them as non-sample
test cases.  Existing test cases are used as the format reference; generated
cases that duplicate an existing (input, output) pair are dropped.

Usage (from the project root, with the project venv):

    venv/bin/python scripts/supplement_test_cases.py --dry-run
    venv/bin/python scripts/supplement_test_cases.py --limit 50
    venv/bin/python scripts/supplement_test_cases.py --source cf --limit 100
    venv/bin/python scripts/supplement_test_cases.py                    # full run

Requires ``DEEPSEEK_API_KEY`` in the environment / ``.env``.

Behaviour:
* Idempotent — problems that already reach ``--min-tests`` are skipped, so
  re-runs only touch still-weak problems.
* Threaded — ``--concurrency`` workers hit the API in parallel (default 8).
* Safe — generated cases are validated structurally (non-empty input/output,
  JSON parse) and deduped against every existing case of the problem before
  insertion.  No case is ever marked ``is_sample``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'oj_project.settings')

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.db import connections, transaction  # noqa: E402
from django.db.models import Count  # noqa: E402

from ai_assistant.deepseek_api import chat_json, DeepSeekError  # noqa: E402
from problems.models import Problem, TestCase  # noqa: E402

PROGRESS_EVERY = 20
DEFAULT_CONCURRENCY = 8
MAX_CONCURRENCY = 32

# Runtime API config (set from CLI args in main()).
_api_key: str = ''
_base_url: str = 'https://api.deepseek.com'
_model: str = 'deepseek-chat'

SYSTEM_PROMPT = (
    'You are a competitive-programming test-case generator.  Given a problem '
    'statement and its existing test cases, produce additional test cases '
    'that follow the EXACT input/output format of the problem.\n'
    'Rules:\n'
    '1. The "output" field MUST be the correct expected output for the given '
    '"input".  Solve the problem mentally before writing each pair.\n'
    '2. Keep inputs small (a few lines at most) so the output is easy to '
    'verify; cover boundary / edge cases mentioned in the statement.\n'
    '3. Do NOT repeat any existing test case (input or input+output).\n'
    '4. Preserve exact whitespace and newline conventions shown in the '
    'existing samples.\n'
    '5. Respond with ONLY a JSON object of the form '
    '{"tests": [{"input": "...", "output": "..."}, ...]}.  No markdown, no '
    'explanation outside the json.'
)

SOURCE_PREFIXES = ('cf:', 'cc:', 'taco:', 'ht:', 'usaco:')


def source_of(tags: str) -> str:
    for t in (tags or '').split(','):
        t = t.strip()
        for p in SOURCE_PREFIXES:
            if t.startswith(p):
                return p[:-1]
    return 'other'


def build_user_prompt(problem: Problem, existing: list[tuple[str, str]],
                      need: int) -> str:
    parts = [f'题目：{problem.title}']
    if problem.difficulty:
        parts.append(f'难度：{problem.difficulty}')
    desc = (problem.description or '').strip()
    if len(desc) > 6000:
        desc = desc[:6000] + '\n...(truncated)'
    parts.append(f'题目描述：\n{desc}')
    if problem.input_format and '见题面' not in problem.input_format:
        parts.append(f'输入格式：\n{problem.input_format}')
    if problem.output_format and '见题面' not in problem.output_format:
        parts.append(f'输出格式：\n{problem.output_format}')
    if problem.hint:
        parts.append(f'提示：\n{problem.hint}')

    parts.append('已有测试用例（作为格式参考，不要重复）：')
    for i, (inp, out) in enumerate(existing, 1):
        parts.append(f'--- 用例 {i} 输入 ---\n{inp}')
        parts.append(f'--- 用例 {i} 输出 ---\n{out}')

    parts.append(
        f'请生成 {need} 个新的测试用例（输入 + 正确的期望输出），'
        '覆盖边界情况，输入尽量短小。只输出 JSON。'
    )
    return '\n\n'.join(parts)


def request_tests(problem: Problem, existing: list[tuple[str, str]],
                  need: int) -> list[tuple[str, str]]:
    """Call DeepSeek and return parsed (input, output) pairs."""
    if not _api_key:
        raise DeepSeekError('DEEPSEEK_API_KEY not configured.')
    user = build_user_prompt(problem, existing, need)
    data = chat_json(
        api_key=_api_key,
        messages=[
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': user},
        ],
        model=_model,
        temperature=0.3,
        max_tokens=1200,
        base_url=_base_url,
        timeout=90,
    )
    tests = data.get('tests') if isinstance(data, dict) else None
    if not isinstance(tests, list):
        return []
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for t in tests:
        if not isinstance(t, dict):
            continue
        inp = str(t.get('input') or '')
        outp = str(t.get('output') or '')
        if not inp.strip() or not outp.strip():
            continue
        pair = (inp, outp)
        if pair in seen:
            continue
        seen.add(pair)
        out.append(pair)
    return out


def supplement_one(problem: Problem, min_tests: int, dry_run: bool) -> dict:
    """Generate and persist extra test cases for one problem.

    Returns a result dict (JSON-safe) with id, before, added, status.
    """
    existing_rows = list(
        TestCase.objects.filter(problem=problem).order_by('order')
        .values_list('input_data', 'expected_output', 'order')
    )
    existing = [((i or ''), (o or '')) for i, o, _ in existing_rows]
    before = len(existing)
    if before >= min_tests:
        return {'id': problem.id, 'before': before, 'added': 0,
                'status': 'already-ok'}

    need = min_tests - before + 2  # +2 spares for dedup / invalid
    try:
        generated = request_tests(problem, existing, need)
    except DeepSeekError as exc:
        return {'id': problem.id, 'before': before, 'added': 0,
                'status': 'api-error', 'error': str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {'id': problem.id, 'before': before, 'added': 0,
                'status': 'error', 'error': repr(exc)}

    existing_set = set(existing)
    existing_inputs = {e[0] for e in existing}
    # Also compare inputs after line-ending normalization, so an AI copy of
    # a sample with \r\n is rejected even if the bytes differ.
    existing_inputs_norm = {
        (e[0] or '').replace('\r\n', '\n').replace('\r', '\n').strip()
        for e in existing
    }
    new_cases: list[tuple[str, str]] = []
    for pair in generated:
        if pair in existing_set:
            continue
        # Skip if the input already exists (same input, different output is
        # risky — we would rather not add a conflicting case).
        if pair[0] in existing_inputs:
            continue
        nin = (pair[0] or '').replace('\r\n', '\n').replace('\r', '\n').strip()
        if nin in existing_inputs_norm:
            continue
        if pair not in new_cases:
            new_cases.append(pair)

    # Only keep as many as needed to reach min_tests.
    new_cases = new_cases[:min_tests - before]
    if not new_cases:
        return {'id': problem.id, 'before': before, 'added': 0,
                'status': 'no-usable'}

    if dry_run:
        return {'id': problem.id, 'before': before,
                'added': len(new_cases), 'status': 'would-add'}

    max_order = max((ord_ for _, _, ord_ in existing_rows), default=-1)
    try:
        with transaction.atomic():
            TestCase.objects.bulk_create([
                TestCase(
                    problem=problem,
                    input_data=inp,
                    expected_output=outp,
                    order=max_order + 1 + i,
                    is_sample=False,
                )
                for i, (inp, outp) in enumerate(new_cases)
            ], batch_size=50)
    except Exception as exc:  # noqa: BLE001
        return {'id': problem.id, 'before': before, 'added': 0,
                'status': 'db-error', 'error': repr(exc)}

    return {'id': problem.id, 'before': before,
            'added': len(new_cases), 'status': 'added'}


def weak_problems(source: str | None, limit: int) -> list[Problem]:
    qs = (Problem.objects.annotate(tc=Count('test_cases'))
          .filter(tc__lt=3).order_by('id'))
    if source:
        qs = qs.filter(tags__contains=f'{source}:')
    if limit:
        ids = list(qs.values_list('id', flat=True)[:limit])
        qs = Problem.objects.filter(id__in=ids)
    return list(qs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--min-tests', type=int, default=3,
                        help='target minimum test cases per problem (default 3)')
    parser.add_argument('--limit', type=int, default=0,
                        help='process at most N problems (0 = all)')
    parser.add_argument('--source', default='',
                        help='only process problems with this source prefix '
                             '(cf/cc/taco/ht/usaco); empty = all')
    parser.add_argument('--concurrency', type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument('--dry-run', action='store_true',
                        help='call the API but do not write to DB')
    parser.add_argument('--api-key', default='',
                        help='API key (default: DEEPSEEK_API_KEY from .env)')
    parser.add_argument('--base-url', default='',
                        help='API base URL (default: https://api.deepseek.com)')
    parser.add_argument('--model', default='',
                        help='model name (default: deepseek-chat)')
    args = parser.parse_args()

    global _api_key, _base_url, _model
    _api_key = (args.api_key or settings.DEEPSEEK_API_KEY or '').strip()
    _base_url = (args.base_url or 'https://api.deepseek.com').rstrip('/')
    _model = (args.model or 'deepseek-chat').strip()
    if not _api_key:
        print('ERROR: no API key (set --api-key or DEEPSEEK_API_KEY)',
              file=sys.stderr)
        return 1

    problems = weak_problems(args.source or None, args.limit)
    print(f'problems to process: {len(problems)} '
          f'(min_tests={args.min_tests}, '
          f'concurrency={args.concurrency}, '
          f'dry_run={args.dry_run})')
    if not problems:
        print('nothing to do.')
        return 0

    concurrency = max(1, min(args.concurrency, MAX_CONCURRENCY))
    stats: Counter = Counter()
    total_added = 0
    started = time.monotonic()
    done = 0

    def worker(problem: Problem) -> dict:
        try:
            return supplement_one(problem, args.min_tests, args.dry_run)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(worker, p): p for p in problems}
        for fut in as_completed(futures):
            res = fut.result()
            done += 1
            stats[res['status']] += 1
            total_added += res.get('added', 0)
            if done % PROGRESS_EVERY == 0 or done == len(problems):
                print(f'  [{done}/{len(problems)}] '
                      f'added={total_added} '
                      f'elapsed={time.monotonic() - started:.0f}s',
                      flush=True)
            if res['status'] in ('api-error', 'error', 'db-error'):
                print(f"  P{res['id']} {res['status']}: "
                      f"{res.get('error', '')}", flush=True)

    print('\n========== summary ==========')
    for status, n in sorted(stats.items()):
        print(f'  {n:>6}  {status}')
    print(f'total test cases added: {total_added}')
    print(f'elapsed: {time.monotonic() - started:.0f}s')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
