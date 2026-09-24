#!/usr/bin/env python
"""Import IOI-style function problems from Hugging Face into guwu-oj.

Sources (CC-BY-4.0):
    https://huggingface.co/datasets/open-r1/ioi             (229 rows)
    https://huggingface.co/datasets/open-r1/ioi-test-cases (per-year subsets)

Each row of ``open-r1/ioi`` is one (year, problem_id, subtask) tuple with
inline ``grader_files`` (a list of ``[filename, content]`` pairs) and a
``starting_code`` stub. The matching test inputs/outputs live in the
sibling ``ioi-test-cases`` dataset, joinable by ``(problem_id, test_name)``.

Subtasks are *not* separate problems here: all rows sharing a
``(year, problem_id)`` are collapsed into a single problem, and the
per-subtask key tag (``ioi:<year>:<problem_id>:<subtask>``) is dropped so
the idempotency key is just ``ioi:<year>:<problem_id>``.

This script:
* Filters out testlib-dependent checker files (we judge by stdout diff, so
  the checker is not needed and would not compile without ``testlib.h``).
* Stores the remaining grader/header files in ``Problem.function_files``
  as a JSON list of ``{"name", "content"}`` dicts.
* Marks each problem ``problem_type='function'`` so ``compile_cpp`` writes
  those files next to the user's ``submission.cpp`` and links them.
* Merges every subtask's ``test_names`` into one union so the problem is
  judged on the full IOI test set.
* Imports test cases from the per-year ``ioi-test-cases`` parquet, joining
  on ``(problem_id, test_name)``.

Both splits of the main dataset are read (``test`` = 2024, ``train`` =
2020-2023); the HF API names every shard ``0.parquet``, so the cache path
keeps the split name to avoid one split clobbering the other.

Usage (from project root, with the project venv):

    venv/bin/python scripts/import_ioi.py --dry-run
    venv/bin/python scripts/import_ioi.py --limit 10
    venv/bin/python scripts/import_ioi.py --year 2023
    venv/bin/python scripts/import_ioi.py --clean-legacy   # drop old per-subtask rows
    venv/bin/python scripts/import_ioi.py

Requires ``pyarrow`` and ``requests`` in the venv.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import os  # noqa: E402  (after sys.path setup)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'oj_project.settings')

import django  # noqa: E402

django.setup()

import pyarrow.parquet as pq  # noqa: E402
import requests  # noqa: E402

from django.contrib.auth import get_user_model  # noqa: E402

from problems.models import Problem, TestCase  # noqa: E402

HF_MAIN = 'open-r1/ioi'
HF_TESTS = 'open-r1/ioi-test-cases'
PARQUET_INDEX_URL_MAIN = f'https://huggingface.co/api/datasets/{HF_MAIN}/parquet'
PARQUET_INDEX_URL_TESTS = f'https://huggingface.co/api/datasets/{HF_TESTS}/parquet'
KEY_TAG_PREFIX = 'ioi:'
DOWNLOAD_CHUNK = 8 * 1024 * 1024
PROGRESS_EVERY = 10
# Cap on the number of test cases per problem. IOI problems can carry 600+
# tests of multiple MB each; pull them all by default (Postgres TextField has
# no hard ceiling), but a per-problem cap keeps a pathological row from
# spending hours in the importer.
MAX_CASES_PER_PROBLEM = 1000
# Test rows are flushed to the DB in chunks while streaming a year's parquet,
# so the process never accumulates a problem's whole test set in memory.
TESTCASE_BATCH = 50

# Columns we need from the main dataset.
MAIN_COLUMNS = [
    'year', 'name', 'id', 'day', 'subtask', 'statement', 'score',
    'time_limit', 'memory_limit', 'task_type', 'test_names',
    'starting_code', 'grader_files', 'problem',
]
# Columns we need from the per-year test-cases dataset.
TEST_COLUMNS = ['year', 'problem_id', 'test_name', 'test_input', 'test_output']


def fetch_parquet_files(dataset_url: str,
                        config: str | None = None) -> list[tuple[str, str]]:
    """Return ``(split, url)`` pairs for parquet shards of the dataset.

    The HF API returns a nested mapping ``{config_name: {split: [urls]}}``;
    we flatten whatever split(s) exist for the requested (or default) config.
    Split names are preserved because every shard is called ``0.parquet`` —
    without them the two splits would collide in the download cache.
    """
    resp = requests.get(dataset_url, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    # Configs can be keyed by name (e.g. "2021") or by 'default'.
    if config:
        cfgs = data.get(config) or {}
    else:
        # Pick the first config (or 'default') — main dataset has only one.
        cfgs = next(iter(data.values())) if data else {}
    out: list[tuple[str, str]] = []
    for split, files in cfgs.items():
        for f in files:
            out.append((split, f if f.startswith('http') else 'https:' + f))
    return out


def download_file(url: str, dest: Path, attempts: int = 5) -> None:
    """Download ``url`` to ``dest``, resuming and retrying on flaky links.

    Some per-year test-case shards are 500-700 MB and the HF CDN drops the
    connection part-way through often enough that a single attempt is not
    reliable. We stream to ``<dest>.part`` and resume with a Range request,
    so a retry costs only the missing bytes.
    """
    if dest.exists() and dest.stat().st_size > 0:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + '.part')
    for attempt in range(1, attempts + 1):
        start = tmp.stat().st_size if tmp.exists() else 0
        try:
            with requests.get(
                url,
                stream=True,
                timeout=(30, 120),
                headers={'Range': f'bytes={start}-'} if start else {},
            ) as r:
                r.raise_for_status()
                if start and r.status_code != 206:
                    # Server ignored the range and is resending from 0.
                    start = 0
                total = int(r.headers.get('content-length', 0)) + start
                done = last_report = start
                with open(tmp, 'ab' if start else 'wb') as fh:
                    for chunk in r.iter_content(chunk_size=DOWNLOAD_CHUNK):
                        fh.write(chunk)
                        done += len(chunk)
                        if total and done - last_report >= 200 * 1024 * 1024:
                            last_report = done
                            print(f'    {done / 1e9:.2f} / {total / 1e9:.2f} GB')
            if not total or done >= total:
                tmp.rename(dest)
                return
            raise IOError(f'short read: {done} of {total} bytes')
        except Exception as exc:  # noqa: BLE001 — retried below
            if attempt == attempts:
                raise
            print(f'    attempt {attempt}/{attempts} failed ({exc}); retrying')
            time.sleep(5 * attempt)


def existing_key_tags() -> set[str]:
    """Key tags of problems already imported by this script."""
    keys: set[str] = set()
    for tags in Problem.objects.filter(
            tags__contains=KEY_TAG_PREFIX).values_list('tags', flat=True):
        for tag in (tags or '').split(','):
            tag = tag.strip()
            if tag.startswith(KEY_TAG_PREFIX):
                keys.add(tag)
    return keys


def is_testlib_checker(name: str, content: str,
                       keep_graders_tree: bool = False) -> bool:
    """True if a grader file must be dropped from ``function_files``.

    The ``checker/`` tree is always dropped: we judge by exact stdout diff,
    so a custom checker is not part of the judging contract.

    For Batch (function) problems any file pulling in ``testlib.h`` is also
    dropped -- the judge image ships no such header, so it would fail to
    compile and poison the link step.

    Interactive (Communication) problems are the opposite: their whole
    ``graders/`` tree is the judging contract (``manager.cpp`` includes
    ``testlib.h``, and ``testlib.h`` itself ships with the task), so
    ``keep_graders_tree`` keeps that tree intact and only strips
    ``checker/``.
    """
    norm = name.replace('\\', '/')
    if norm.startswith('./'):
        norm = norm[2:]
    bare = norm.rsplit('/', 1)[-1]
    if norm.startswith('checker/') or bare == 'checker.cpp' \
       or bare == 'checker.h':
        return True
    if keep_graders_tree:
        return False
    if 'testlib.h' in content:
        return True
    return False


def filter_grader_files(grader_files, keep_graders_tree: bool = False):
    """Return list of {name, content} dicts with unusable graders removed.

    The dataset stores grader_files as a list of ``[name, content]`` pairs;
    output is the shape ``Problem.function_files`` expects. Pass
    ``keep_graders_tree=True`` for interactive problems (see
    :func:`is_testlib_checker`).
    """
    out = []
    for entry in grader_files or []:
        if not entry or len(entry) < 2:
            continue
        name = str(entry[0] or '').strip()
        content = str(entry[1] or '')
        if not name:
            continue
        if is_testlib_checker(name, content, keep_graders_tree):
            continue
        out.append({'name': name, 'content': content})
    return out


def build_tags(year: int, problem_id: str) -> str:
    """Idempotency key first, then provenance + year + problem id.

    The key deliberately carries no subtask: all subtask rows of a problem
    are merged into one problem, so ``ioi:2024:nile:07-full`` style tags are
    never written.
    """
    key_tag = f'{KEY_TAG_PREFIX}{year}:{problem_id}'
    parts = [key_tag, 'IOI', f'IOI{year}', f'ioi:{problem_id}']
    # CharField(200) — keep the head, drop the tail if too long.
    while len(','.join(parts)) > 200 and len(parts) > 1:
        parts.pop()
    return ','.join(parts)[:200]


def merge_rows(rows: list[dict]) -> tuple[dict, list[str]]:
    """Collapse a problem's subtask rows into (primary row, union test names).

    The statement, limits and grader files are shared across subtasks, so we
    use the most complete row (the ``*full`` subtask when present, otherwise
    the longest statement) and union every subtask's ``test_names`` so the
    merged problem is judged on all IOI test points.
    """
    def rank(row: dict):
        subtask = str(row.get('subtask') or '').strip().lower()
        return (
            0 if subtask.endswith('full') else 1,
            -len(row.get('statement') or ''),
            subtask,
        )

    primary = min(rows, key=rank)
    seen: set[str] = set()
    test_names: list[str] = []
    for row in sorted(rows, key=lambda r: str(r.get('subtask') or '')):
        for tname in row.get('test_names') or []:
            tname = str(tname or '').strip()
            if tname and tname not in seen:
                seen.add(tname)
                test_names.append(tname)
    return primary, test_names


def legacy_key_tag(tag: str) -> bool:
    """True for old per-subtask key tags ``ioi:<year>:<problem_id>:<subtask>``."""
    parts = tag.split(':')
    return len(parts) >= 4 and parts[0] == KEY_TAG_PREFIX.rstrip(':')


def clean_legacy_problems(dry_run: bool) -> int:
    """Delete problems imported by the old subtask-split scheme.

    Those rows carry a 4-part key tag (``ioi:2024:nile:07-full``); the merged
    scheme writes 3-part keys, so leaving them behind would duplicate every
    problem that has subtasks.
    """
    stale: list[int] = []
    for pk, tags in Problem.objects.filter(
            tags__contains=KEY_TAG_PREFIX).values_list('id', 'tags'):
        for tag in (tags or '').split(','):
            if legacy_key_tag(tag.strip()):
                stale.append(pk)
                break
    if stale and not dry_run:
        Problem.objects.filter(id__in=stale).delete()
    return len(stale)


def build_description(row, interactive: bool = False) -> str:
    """Assemble the markdown statement shown on the problem page."""
    statement = (row.get('statement') or '').strip()
    problem = (row.get('problem') or '').strip()
    parts = [p for p in (statement, problem) if p]
    description = '\n\n'.join(parts).strip()
    starting = (row.get('starting_code') or '').strip()
    if starting:
        if interactive:
            description += (
                '\n\n## 参考代码\n\n```cpp\n'
                + starting
                + '\n```\n\n请将你的实现写入 `submission.cpp`（可沿用上面的框架与'
                '题目头文件）。判题时你的代码会与题目自带的 manager / grader 一起'
                '编译链接，并按题目规定的协议通信。'
            )
        else:
            description += (
                '\n\n## 参考函数签名\n\n```cpp\n'
                + starting
                + '\n```\n\n请在 `submission.cpp` 中实现该函数（可 `#include "problem.h"` '
                '获取题目提供的声明）。判题时你的代码会与题目提供的 grader 一起编译链接。'
            )
    return description


def is_interactive_row(row) -> bool:
    """True for a CMS ``Communication`` row (an interactive problem)."""
    return (row.get('task_type') or '').strip() == 'Communication'


def interactive_config_from(row, grader_files) -> dict:
    """Derive ``interactive_config`` from the task's ``grader_config.json``.

    ``num_processes`` defaults to 1. ``user_io`` is the Communication I/O
    protocol; a few tasks (e.g. robot) hand the answer over as a regular
    file that the manager opens with ``ifstream`` rather than a FIFO, which
    the harness runs sequentially as ``file_io``.
    """
    cfg: dict = {}
    for f in grader_files or []:
        if str(f.get('name') or '').endswith('grader_config.json'):
            try:
                cfg = json.loads(f.get('content') or '{}') or {}
            except (TypeError, ValueError):
                cfg = {}
            break
    params = cfg.get('task_type_params') or {}
    try:
        num_processes = int(
            params.get('task_type_parameters_Communication_num_processes') or 1)
    except (TypeError, ValueError):
        num_processes = 1
    num_processes = max(num_processes, 1)
    user_io = str(
        params.get('task_type_parameters_Communication_user_io') or 'fifo_io')
    if user_io not in ('fifo_io', 'std_io'):
        user_io = 'fifo_io'
    mgr = next((f.get('content') or '' for f in (grader_files or [])
                if str(f.get('name') or '').endswith('manager.cpp')), '')
    if user_io == 'fifo_io' and 'ifstream' in mgr and 'fopen(' not in mgr:
        user_io = 'file_io'
    return {'num_processes': num_processes, 'user_io': user_io}


def iter_year_tests(tests_cache_dir: Path, year: int):
    """Yield ``(problem_id, test_name, input, output)`` for one year.

    Rows are yielded from a server-side cursor instead of being collected:
    a year's shard is 400-900 MB compressed and a single problem can carry
    tens of MB per test, so materialising the whole year (as a
    ``{(pid, name): (in, out)}`` dict) grows the process past 7 GB and gets
    it OOM-killed. Callers must stream and flush as they go.
    """
    for _split, url in fetch_parquet_files(PARQUET_INDEX_URL_TESTS, config=str(year)):
        fname = tests_cache_dir / str(year) / url.rsplit('/', 1)[-1]
        download_file(url, fname)
        pf = pq.ParquetFile(fname)
        # Tiny batches: each row can be tens of MB once decompressed.
        for batch in pf.iter_batches(batch_size=4, columns=TEST_COLUMNS):
            for row in batch.to_pylist():
                problem_id = str(row.get('problem_id') or '').strip()
                test_name = str(row.get('test_name') or '').strip()
                if not problem_id or not test_name:
                    continue
                yield (problem_id, test_name,
                       row.get('test_input') or '', row.get('test_output') or '')


def create_problem(year: int, problem_id: str, row: dict,
                   grader_files: list[dict], creator,
                   interactive: bool = False,
                   interactive_config: dict | None = None) -> Problem:
    """Create the merged Problem row; test cases are inserted afterwards."""
    name = str(row.get('name') or problem_id).strip()
    time_limit_ms = max(
        int(round(float(row.get('time_limit') or 1.0) * 1000)), 100)
    memory_limit_mb = max(
        int(int(row.get('memory_limit') or 256 * 1024 * 1024) // (1024 * 1024)),
        16)
    if interactive:
        problem_type = 'interactive'
        input_format = ('本题为交互题：你的程序需与题目自带的 manager 进程通信'
                        '（按题目约定经标准输入输出或命名管道），无需自行读取测试数据。')
        output_format = '由 manager 进程打印判定结果，无需自行写出答案。'
        config_json = json.dumps(interactive_config or {})
    else:
        problem_type = 'function'
        input_format = '本题是函数题，由 grader 调用你实现的函数。'
        output_format = '由 grader 打印判定结果，无需自行读写 stdin/stdout。'
        config_json = '{}'
    return Problem.objects.create(
        title=f'[IOI {year}] {name}'[:200],
        description=build_description(row, interactive),
        input_format=input_format,
        output_format=output_format,
        sample_input='',
        sample_output='',
        hint=(row.get('starting_code') or '').strip(),
        difficulty='NOI',
        time_limit=time_limit_ms,
        memory_limit=memory_limit_mb,
        tags=build_tags(year, problem_id),
        created_by=creator,
        is_public=False,
        problem_type=problem_type,
        interactive_config=config_json,
        function_files=json.dumps(grader_files),
    )


def skip_reason(row, args) -> str | None:
    """Return a reason string to skip this row, or None to import it."""
    task_type = (row.get('task_type') or '').strip()
    if task_type and task_type not in ('Batch', 'Communication'):
        return f'task_type={task_type} (unsupported)'
    if not (row.get('statement') or '').strip() and \
       not (row.get('problem') or '').strip():
        return 'empty statement'
    if not (row.get('grader_files') or []):
        return 'no grader files'
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--user', default='oscar',
                        help='username credited as problem creator (default: oscar)')
    parser.add_argument('--limit', type=int, default=0,
                        help='import at most N problems (0 = no limit)')
    parser.add_argument('--year', type=int, default=0,
                        help='only import problems from this year (0 = all)')
    parser.add_argument('--dry-run', action='store_true',
                        help='report filter statistics only, no DB writes')
    parser.add_argument('--clean-legacy', action='store_true',
                        help='delete problems imported by the old per-subtask '
                             'scheme before importing merged ones')
    parser.add_argument(
        '--cache-dir',
        default=str(PROJECT_ROOT / 'scripts' / '.cache' / 'ioi'),
    )
    args = parser.parse_args()

    User = get_user_model()
    try:
        creator = User.objects.get(username=args.user)
    except User.DoesNotExist:
        print(f'ERROR: user "{args.user}" does not exist.', file=sys.stderr)
        return 1

    cache_dir = Path(args.cache_dir)
    main_cache = cache_dir / 'main'
    tests_cache = cache_dir / 'tests'

    stats: Counter = Counter()
    started = time.monotonic()

    # 1) Download + scan the main ioi dataset (~270 rows across both splits).
    main_urls = fetch_parquet_files(PARQUET_INDEX_URL_MAIN)
    main_files: list[Path] = []
    for split, url in main_urls:
        # HF names every shard 0.parquet; keep the split in the path so the
        # test and train shards don't overwrite each other.
        fname = main_cache / split / url.rsplit('/', 1)[-1]
        download_file(url, fname)
        main_files.append(fname)

    rows: list[dict] = []
    for fname in main_files:
        print(f'scanning {fname.parent.name}/{fname.name} ...')
        pf = pq.ParquetFile(fname)
        for batch in pf.iter_batches(batch_size=64, columns=MAIN_COLUMNS):
            for row in batch.to_pylist():
                rows.append(row)
    print(f'  total rows: {len(rows)}')

    # 2) Collapse the per-subtask rows into one group per (year, problem_id).
    groups: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for row in rows:
        year = int(row.get('year') or 0)
        if args.year and year != args.year:
            continue
        problem_id = str(row.get('id') or '').strip()
        if not problem_id:
            stats['missing id'] += 1
            continue
        groups[(year, problem_id)].append(row)
    print(f'  {len(groups)} distinct problems, '
          f'{len(rows)} subtask rows')

    if args.clean_legacy:
        cleaned = clean_legacy_problems(args.dry_run)
        print(f'  {"would delete" if args.dry_run else "deleted"} '
              f'{cleaned} legacy per-subtask problems')

    imported_keys = existing_key_tags()
    imported = 0

    # 3) Per year: create the merged problems first, then make a single
    #    streaming pass over that year's test cases, routing each test to its
    #    problem and flushing in batches. Holding a whole year in memory (as
    #    the old {(pid, name): (in, out)} dict) costs 7 GB+ and gets the
    #    process OOM-killed, so nothing here is materialised.
    for year in sorted({y for y, _ in groups}):
        year_groups = {pid: g for (y, pid), g in groups.items() if y == year}
        print(f'\n=== year {year}: {len(year_groups)} problems ===')

        plans: dict[str, tuple[dict, list[str], list[dict], bool]] = {}
        for problem_id in sorted(year_groups):
            key_tag = f'{KEY_TAG_PREFIX}{year}:{problem_id}'
            if key_tag in imported_keys:
                stats['already imported'] += 1
                continue

            usable: list[dict] = []
            reasons: list[str] = []
            for row in year_groups[problem_id]:
                reason = skip_reason(row, args)
                if reason:
                    reasons.append(reason)
                else:
                    usable.append(row)
            if not usable:
                stats[reasons[0] if reasons else 'no usable subtask row'] += 1
                continue

            row, test_names = merge_rows(usable)
            if not test_names:
                stats['no test names'] += 1
                continue

            interactive = is_interactive_row(row)
            grader_files = filter_grader_files(
                row.get('grader_files'), keep_graders_tree=interactive)
            if not grader_files or not any(
                    f['name'].endswith('.cpp') for f in grader_files):
                stats['no usable grader .cpp (after testlib filter)'] += 1
                continue

            plans[problem_id] = (row, test_names, grader_files, interactive)

        if args.limit:
            remaining = args.limit - imported
            if remaining <= 0:
                print(f'--limit {args.limit} reached, stopping.')
                break
            if len(plans) > remaining:
                plans = {pid: plans[pid] for pid in sorted(plans)[:remaining]}

        if not plans:
            continue

        # One streaming sink per problem still looking for its tests.
        pending: dict[str, dict] = {}
        for pid, (row, test_names, grader_files, interactive) in plans.items():
            iconfig = (interactive_config_from(row, grader_files)
                       if interactive else None)
            state = {
                'names': set(test_names),   # test names not seen yet
                'problem': None,
                'buffer': [],
                'count': 0,
                'sample': None,
                'failed': False,
            }
            if not args.dry_run:
                try:
                    state['problem'] = create_problem(
                        year, pid, row, grader_files, creator,
                        interactive=interactive, interactive_config=iconfig)
                except Exception as exc:  # noqa: BLE001 — skip this problem
                    stats['DB error'] += 1
                    print(f'  ERROR creating {KEY_TAG_PREFIX}{year}:{pid}: {exc}',
                          file=sys.stderr)
                    continue
            pending[pid] = state

        try:
            for pid, tname, tin, tout in iter_year_tests(tests_cache, year):
                state = pending.get(pid)
                if state is None or tname not in state['names']:
                    continue
                state['names'].discard(tname)
                state['count'] += 1
                if args.dry_run or state['failed']:
                    continue
                if state['sample'] is None:
                    state['sample'] = (tin, tout)
                if state['count'] > MAX_CASES_PER_PROBLEM:
                    continue
                state['buffer'].append(TestCase(
                    problem=state['problem'],
                    input_data=tin,
                    expected_output=tout,
                    order=state['count'] - 1,
                    is_sample=tname.startswith('0'),
                ))
                if len(state['buffer']) >= TESTCASE_BATCH:
                    try:
                        TestCase.objects.bulk_create(state['buffer'], batch_size=50)
                    except Exception as exc:  # noqa: BLE001
                        stats['DB error'] += 1
                        print(f'  ERROR inserting tests for '
                              f'{KEY_TAG_PREFIX}{year}:{pid}: {exc}',
                              file=sys.stderr)
                        state['failed'] = True
                    state['buffer'].clear()
        except Exception as exc:  # noqa: BLE001 — flaky shard, next year still runs
            stats['test-cases fetch error'] += 1
            print(f'  ERROR reading test cases for {year}: {exc}', file=sys.stderr)

        for pid in sorted(pending):
            state = pending[pid]
            key_tag = f'{KEY_TAG_PREFIX}{year}:{pid}'
            if args.dry_run:
                stats['would import' if state['count']
                      else 'no test cases resolved'] += 1
                continue
            problem = state['problem']
            if state['failed'] or not state['count']:
                # Nothing usable stored: drop the half-built row rather than
                # leave an untested shadow problem behind (cascades to tests).
                try:
                    problem.delete()
                except Exception:  # noqa: BLE001
                    pass
                if not state['failed']:
                    stats['no test cases resolved'] += 1
                continue
            try:
                if state['buffer']:
                    TestCase.objects.bulk_create(state['buffer'], batch_size=50)
                    state['buffer'].clear()
                if state['sample']:
                    problem.sample_input = state['sample'][0][:8192]
                    problem.sample_output = state['sample'][1][:8192]
                    problem.save(update_fields=['sample_input', 'sample_output'])
            except Exception as exc:  # noqa: BLE001
                problem.delete()
                stats['DB error'] += 1
                print(f'  ERROR finalising {key_tag}: {exc}', file=sys.stderr)
                continue
            imported_keys.add(key_tag)
            imported += 1
            unresolved = len(state['names'])
            print(f'  {key_tag}: {state["count"]} tests'
                  + (f' ({unresolved} test names unresolved)' if unresolved else ''))
            if imported % PROGRESS_EVERY == 0:
                print(f'  imported {imported} problems '
                      f'({time.monotonic() - started:.0f}s)')

        if args.limit and imported >= args.limit:
            print(f'--limit {args.limit} reached, stopping.')
            break

    _report(args, stats, imported, started)
    return 0


def _report(args, stats: Counter, imported: int, started: float) -> None:
    print('\n========== summary ==========')
    print(f'mode: {"DRY RUN" if args.dry_run else "IMPORT"} | user: {args.user}')
    for reason, n in stats.most_common():
        print(f'  {n:>6}  {reason}')
    print(f'imported: {imported} | elapsed: {time.monotonic() - started:.0f}s')


if __name__ == '__main__':
    raise SystemExit(main())
