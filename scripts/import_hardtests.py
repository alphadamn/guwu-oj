#!/usr/bin/env python
"""Import problems + test cases from the Hugging Face dataset
``sigcp/hardtests_problems`` (HARDTESTS, 47k problems from 13 OJ platforms)
into the guwu-oj database.

    https://huggingface.co/datasets/sigcp/hardtests_problems
    Paper: arxiv 2505.24098 (HARDTESTS)

The dataset collects competitive-programming problems from AtCoder,
Codeforces, Luogu, SPOJ, UVa, Aizu, GeeksforGeeks, and other OJ platforms,
each with public test cases (stdin/stdout).  Problem statements come from
Luogu (for SPOJ/UVa/Luogu) or the original platform.

Shards are read from a LOCAL cache (download once):

    mkdir -p scripts/.cache/hardtests
    for i in $(seq 0 11); do
      curl -L -o scripts/.cache/hardtests/train-$i.parquet \\
        "https://huggingface.co/api/datasets/sigcp/hardtests_problems/parquet/default/train/$i.parquet"
    done

Memory: a two-pass design (mirroring import_taco.py) keeps the host safe —
some rows carry several MB of solutions.  Pass 1 stores only fingerprints /
metadata; pass 2 re-scans one shard at a time and materialises just the rows
selected for import.  The ``solutions`` column is never projected.

Usage:

    venv/bin/python scripts/import_hardtests.py --dry-run
    venv/bin/python scripts/import_hardtests.py --limit 100
    venv/bin/python scripts/import_hardtests.py --target-total 30000

Idempotency: every problem carries a ``ht:<sha1(url or pid)>`` tag.
Cross-dataset duplicates are filtered via:
  * Codeforces pid → ``cf:<contest>/<index>`` key tag match (fast path)
  * Core-statement fingerprint (head + full) against every existing problem
  * Same-platform normalized title match
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import html
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import os  # noqa: E402

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'oj_project.settings')

import django  # noqa: E402

django.setup()

import pyarrow.parquet as pq  # noqa: E402

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import transaction  # noqa: E402

from problems.models import Problem, TestCase  # noqa: E402

CACHE_DIR = PROJECT_ROOT / 'scripts' / '.cache' / 'hardtests'
KEY_TAG_PREFIX = 'ht:'
CF_KEY_TAG_PREFIX = 'cf:'
PROGRESS_EVERY = 100
# NEVER project the ``solutions`` column — some rows have 22k solutions.
NEEDED_COLUMNS = [
    'pid', 'question_title', 'question_content', 'platform',
    'public_test_cases', 'time_limit', 'memory_limit', 'url',
    'difficulty_ratings', 'tags',
]

# ---- normalisation helpers (shared with import_taco.py style) -------------

HTML_TAG_RE = re.compile(r'<[^>]+>')
NON_ALNUM_RE = re.compile(r'[^a-z0-9]+')
URL_LINE_RE = re.compile(r'\[problemUrl\]:\s*\S+')
HEADER_LINE_RE = re.compile(r'^#+\s*', re.MULTILINE)
SECTION_RE = re.compile(
    r'^(?:Problem Statement|Problem Description|Background|Description|'
    r'Input|Output|Constraints|Sample Input|Sample Output|Sample|'
    r'Hint|Note|Explanation|入力|出力|制約|説明|問題|問題文|入出力|'
    r'Input Format|Output Format|Score|Scoring)\s*$',
    re.MULTILINE | re.IGNORECASE,
)
# Cut at first occurrence of an I/O / constraints section (end of body).
CUT_RE = re.compile(
    r'\n\s*(?:#+\s*)?(?:Input|入力|Output|出力|Constraints|制約|'
    r'Sample Input|Sample Output|入出力|Input Format|Output Format|'
    r'Score|Scoring)\s*[\n:]',
    re.IGNORECASE,
)
INTERACTIVE_RE = re.compile(
    r'(?:this is an? interactive (?:problem|task)|'
    r'インタラクティブな問題|インタラクティブです|'
    r'Interaction|Interactive)',
    re.IGNORECASE,
)
TITLE_PREFIX_RE = re.compile(r'^\[[^\]]+\]\s*')


def core_statement(text: str) -> str:
    """Return the alphanumeric-only lowercase *body* of the statement,
    with all metadata (URLs, markdown headers, section labels) stripped and
    everything after the first Input/Output section cut away."""
    text = text or ''
    text = URL_LINE_RE.sub('', text)
    text = HEADER_LINE_RE.sub('', text)  # remove "# " prefixes
    text = SECTION_RE.sub('', text)  # drop standalone section labels
    text = html.unescape(text)
    text = HTML_TAG_RE.sub(' ', text)
    m = CUT_RE.search(text)
    if m:
        text = text[:m.start()]
    return NON_ALNUM_RE.sub('', text.lower()).strip()


def fp_head(text: str) -> str:
    return hashlib.sha1(text[:600].encode()).hexdigest()[:16]


def fp_full(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def normalize_title(title: str) -> str:
    title = TITLE_PREFIX_RE.sub('', title or '').strip().lower()
    return NON_ALNUM_RE.sub('', html.unescape(title))


# ---- codeforces pid → cf: key tag -----------------------------------------

CF_PID_RE = re.compile(r'^codeforces_(\d+)_([a-zA-Z0-9]+)$')
LUOGU_URL_RE = re.compile(r'/problem/(P\d+)', re.IGNORECASE)


def cf_key_tag_from_pid(pid: str) -> str | None:
    """Map a sigcp pid like ``codeforces_852_a`` to ``cf:852/A``."""
    m = CF_PID_RE.match(pid or '')
    if not m:
        return None
    return f'{CF_KEY_TAG_PREFIX}{m.group(1)}/{m.group(2).upper()}'


def luogu_pid_from_url(url: str) -> str | None:
    """Extract the Luogu P-code (e.g. ``P2028``) from a sigcp URL."""
    m = LUOGU_URL_RE.search(url or '')
    if not m:
        return None
    return m.group(1).upper()


# ---- difficulty / limit parsing -------------------------------------------

DIFF_MAP = {
    'easy': '入门', 'school': '入门', 'beginner': '入门', 'basic': '入门',
    'simple': '入门',
    'medium': '普及', 'medium_easy': '普及-', 'easy_medium': '普及-',
    'medium_hard': '提高-', 'moderate': '提高-',
    'hard': '提高', 'harder': '提高+',
    'very hard': '提高+', 'very_hard': '提高+',
    'challenge': '省选', 'challenging': '省选', 'expert': '省选',
    'super_hard': '省选',
    'unknown': '普及', '': '普及',
}

# Luogu difficulty colours → site difficulty
LUOGU_DIFF_MAP = {
    '暂无评定': '普及', '入门': '入门', '普及-': '普及-',
    '普及': '普及', '普及+': '普及+', '提高-': '提高-',
    '提高': '提高', '提高+': '提高+', '省选-': '省选',
    '省选': '省选', '省选+': '省选', 'NOI': 'NOI',
    'NOI+': 'NOI', 'CTSC': 'NOI',
}


def difficulty_for(row: dict) -> str:
    ratings = row.get('difficulty_ratings') or []
    for r in ratings:
        src = (r.get('source') or '').lower()
        level = (r.get('level') or '').strip().lower()
        score = r.get('score')
        if src == 'luogu' and level:
            # Luogu uses Chinese labels directly.
            mapped = LUOGU_DIFF_MAP.get(level)
            if mapped:
                return mapped
            # Luogu also uses colour → score 0..7
            if score is not None and 0 <= score <= 7:
                return ['入门', '普及-', '普及', '普及+', '提高-',
                        '提高', '提高+', '省选'][min(score, 7)]
        if level:
            mapped = DIFF_MAP.get(level)
            if mapped:
                return mapped
    return '普及'


def parse_limits(tl: str, ml: str) -> tuple[int, int]:
    tl_ms, mem_mb = 0, 0
    m = re.search(r'([\d.]+)\s*(milliseconds?|ms|seconds?|sec|s)\b',
                  (tl or '').lower())
    if m:
        value = float(m.group(1))
        unit = m.group(2)
        tl_ms = int(value * 1000) if unit.startswith(('s', 'sec')) \
            else int(value)
    m = re.search(r'([\d.]+)\s*(kilobytes?|kb|megabytes?|mb|gigabytes?|gb)\b',
                  (ml or '').lower())
    if m:
        value = float(m.group(1))
        unit = m.group(2)
        if unit.startswith('k'):
            mem_mb = int(value / 1024)
        elif unit.startswith('m'):
            mem_mb = int(value)
        elif unit.startswith('g'):
            mem_mb = int(value * 1024)
    return tl_ms, mem_mb


# ---- platform label -------------------------------------------------------

PLATFORM_LABEL = {
    'atcoder': 'AtCoder',
    'codeforces': 'Codeforces',
    'luogu': '洛谷',
    'spoj': 'SPOJ',
    'uva': 'UVa',
    'aizu': 'AOJ',
    'geeksforgeeks': 'GeeksforGeeks',
    'hackerrank': 'HackerRank',
    'hackerearth': 'HackerEarth',
    'codechef': 'CodeChef',
    'nowcoder': 'NowCoder',
    'poj': 'POJ',
    'hdu': 'HDU',
    'unknown': 'Unknown',
}


def platform_label(platform: str) -> str:
    p = (platform or '').strip().lower()
    return PLATFORM_LABEL.get(p, p or 'Unknown')


# ---- pass 1: light metadata -----------------------------------------------

def shard_paths() -> list[Path]:
    return sorted(CACHE_DIR.glob('train-*.parquet'))


def classify_row(row: dict) -> tuple[dict | None, str | None]:
    """Return (light_meta, skip_reason) for one parquet row."""
    platform = (row.get('platform') or '').strip().lower()
    if not platform:
        return None, 'skip: no platform'

    question = (row.get('question_content') or '').strip()
    if not question or len(question) < 20:
        return None, 'skip: empty question'

    tests = row.get('public_test_cases') or []
    if not tests:
        return None, 'skip: no test cases'
    # All tests must be stdin/stdout (no function-call / file I/O).
    if not all(isinstance(t, dict) and
               (t.get('testtype') or 'stdin') == 'stdin' for t in tests):
        return None, 'skip: non-stdio test type'
    # At least one test must have a non-empty expected output.
    if not any(str(t.get('output') or '').strip() for t in tests
               if isinstance(t, dict)):
        return None, 'skip: all outputs empty'

    # Interactive problems can't be judged by exact-match stdin.
    if INTERACTIVE_RE.search(question[:800]):
        return None, 'skip: interactive'

    pid = (row.get('pid') or '').strip()
    url = (row.get('url') or '').strip()
    key_tag = KEY_TAG_PREFIX + hashlib.sha1(
        (url or pid).encode()).hexdigest()[:16]

    title = (row.get('question_title') or '').strip() or \
        (question.split('\n', 1)[0][:80] if question else 'Untitled')

    tl_ms, mem_mb = parse_limits(row.get('time_limit'),
                                row.get('memory_limit'))
    core = core_statement(question)
    meta = {
        'platform': platform,
        'pid': pid,
        'url': url,
        'title': title[:200],
        'difficulty': difficulty_for(row),
        'time_limit': max(tl_ms, 100) if tl_ms else 2000,
        'memory_limit': max(mem_mb, 16) if mem_mb else 256,
        'key_tag': key_tag,
        'n_tests': len(tests),
        'fp_head': fp_head(core) if core else '',
        'fp_full': fp_full(core) if core else '',
        'norm_title': normalize_title(title),
        'cf_key': cf_key_tag_from_pid(pid),  # None for non-CF
        'luogu_pid': luogu_pid_from_url(url),  # None for non-Luogu
    }
    return meta, None


def scan_light(min_tests: int) -> tuple[dict[str, dict], Counter]:
    """Pass 1: metadata only (key_tag -> meta), no statement/test blobs."""
    stats: Counter = Counter()
    metas: dict[str, dict] = {}
    for path in shard_paths():
        try:
            pf = pq.ParquetFile(path, pre_buffer=False)
        except Exception as exc:  # noqa: BLE001
            print(f'  WARN: cannot open {path.name}: {exc}', flush=True)
            stats['corrupt shard'] += 1
            continue
        file_rows = 0
        for batch in pf.iter_batches(batch_size=256, columns=NEEDED_COLUMNS):
            for row in batch.to_pylist():
                stats['rows'] += 1
                file_rows += 1
                meta, reason = classify_row(row)
                if meta is None:
                    stats[reason] += 1
                    continue
                if meta['n_tests'] < min_tests:
                    stats[f'skip: fewer than {min_tests} tests'] += 1
                    continue
                if meta['key_tag'] in metas:
                    stats['skip: duplicate within HARDTESTS'] += 1
                    continue
                meta['shard'] = path.name
                metas[meta['key_tag']] = meta
                stats[f'candidate:{meta["platform"]}'] += 1
        print(f'  pass1 {path.name}: {file_rows} rows, '
              f'{len(metas)} candidates', flush=True)
        del pf
        gc.collect()
    return metas, stats


# ---- existing DB index ----------------------------------------------------

def build_existing_index() -> tuple[set[str], set[str], set[str],
                                      set[str], dict[str, set[str]]]:
    """Return (ht key tags, cf key tags, statement fingerprints,
    luogu pids, {platform label: {normalized titles}}) over every
    existing problem."""
    ht_keys: set[str] = set()
    cf_keys: set[str] = set()
    fps: set[str] = set()
    luogu_pids_db: set[str] = set()
    titles_by_label: dict[str, set[str]] = {}
    for tags, title, desc, lp in Problem.objects.values_list(
            'tags', 'title', 'description', 'luogu_pid'):
        for tag in (tags or '').split(','):
            tag = tag.strip()
            if tag.startswith(KEY_TAG_PREFIX):
                ht_keys.add(tag)
            elif tag.startswith(CF_KEY_TAG_PREFIX):
                cf_keys.add(tag)
        core = core_statement(desc)
        if core:
            fps.add(fp_head(core))
            fps.add(fp_full(core))
        if lp:
            luogu_pids_db.add(lp.upper())
        m = re.match(r'^\[([^\]]+)\]\s*(.*)$', title or '')
        if m:
            titles_by_label.setdefault(m.group(1), set()).add(
                normalize_title(m.group(2)))
    return ht_keys, cf_keys, fps, luogu_pids_db, titles_by_label


def select_for_import(metas: dict[str, dict], stats: Counter,
                      skip_platforms: set[str]) -> list[str]:
    ht_keys, cf_keys, fps, luogu_pids_db, titles_by_label = \
        build_existing_index()
    plan: list[str] = []
    seen_fp: set[str] = set()
    for key, cand in metas.items():
        if key in ht_keys:
            stats['dup: ht key already in DB'] += 1
            continue
        if cand['platform'] in skip_platforms:
            stats['skip: platform excluded'] += 1
            continue
        # Fast path for Codeforces: cf: key tag match.
        if cand['cf_key'] and cand['cf_key'] in cf_keys:
            stats['dup: cf key tag'] += 1
            continue
        # Fast path for Luogu: P-code match against DB luogu_pid.
        if cand['luogu_pid'] and cand['luogu_pid'] in luogu_pids_db:
            stats['dup: luogu pid'] += 1
            continue
        # Statement fingerprint against ALL existing problems.
        if cand['fp_head'] and cand['fp_head'] in fps:
            stats['dup: statement fingerprint (head)'] += 1
            continue
        if cand['fp_full'] and cand['fp_full'] in fps:
            stats['dup: statement fingerprint (full)'] += 1
            continue
        # Same-platform title match (catches translated / reformatted dupes).
        label = platform_label(cand['platform'])
        if cand['norm_title'] and cand['norm_title'] in \
                titles_by_label.get(label, set()):
            stats['dup: same-platform title'] += 1
            continue
        # Within-batch fingerprint dedup.
        if cand['fp_head'] and cand['fp_head'] in seen_fp:
            stats['dup: repeated fingerprint in batch'] += 1
            continue
        if cand['fp_head']:
            seen_fp.add(cand['fp_head'])
        plan.append(key)
    # Best-first: more tests first, then by platform preference.
    plan.sort(key=lambda k: (-metas[k]['n_tests'], metas[k]['platform']))
    return plan


# ---- pass 2: materialise + insert ----------------------------------------

def _insert_row(meta: dict, row: dict, creator) -> None:
    tests = row.get('public_test_cases') or []
    parsed = []
    for t in tests:
        if not isinstance(t, dict):
            continue
        inp = str(t.get('input') or '')
        out = str(t.get('output') or '')
        if not any(o.strip() for _, o in parsed) and not out.strip():
            continue
        parsed.append((inp, out))
    if not parsed or not any(o.strip() for _, o in parsed):
        raise ValueError('all outputs empty')

    label = platform_label(meta['platform'])
    with transaction.atomic():
        problem = Problem.objects.create(
            title=f'[{label}] {meta["title"]}'[:200],
            description=(row.get('question_content') or '').strip(),
            input_format='见题面 Input 部分',
            output_format='见题面 Output 部分',
            sample_input=parsed[0][0],
            sample_output=parsed[0][1],
            hint='',
            difficulty=meta['difficulty'],
            time_limit=meta['time_limit'],
            memory_limit=meta['memory_limit'],
            tags=f'{meta["key_tag"]},{label}'[:200],
            created_by=creator,
            is_public=True,
        )
        TestCase.objects.bulk_create([
            TestCase(problem=problem, input_data=i_, expected_output=o_,
                     order=j, is_sample=j < 2)
            for j, (i_, o_) in enumerate(parsed)
        ], batch_size=500)


def run_import(metas: dict[str, dict], plan: list[str], quota: int,
               username: str) -> Counter:
    """Pass 2: re-scan shards, materialise only the planned rows."""
    stats: Counter = Counter()
    User = get_user_model()
    creator = User.objects.get(username=username)
    planned = set(plan[:quota])
    order_by_shard: dict[str, list[str]] = {}
    for key in plan[:quota]:
        order_by_shard.setdefault(metas[key]['shard'], []).append(key)
    remaining = dict(order_by_shard)

    imported = 0
    started = time.monotonic()
    for path in shard_paths():
        wanted = remaining.get(path.name)
        if not wanted:
            continue
        wanted_set = set(wanted)
        materialized: dict[str, dict] = {}
        try:
            pf = pq.ParquetFile(path, pre_buffer=False)
        except Exception as exc:  # noqa: BLE001
            print(f'  WARN: cannot open {path.name} in pass2: {exc}',
                  flush=True)
            stats['pass2: corrupt shard'] += 1
            continue
        for batch in pf.iter_batches(batch_size=128, columns=NEEDED_COLUMNS):
            for row in batch.to_pylist():
                pid = (row.get('pid') or '').strip()
                url = (row.get('url') or '').strip()
                key = KEY_TAG_PREFIX + hashlib.sha1(
                    (url or pid).encode()).hexdigest()[:16]
                if key in wanted_set:
                    materialized[key] = row
        del pf
        gc.collect()
        for key in wanted:
            row = materialized.get(key)
            if row is None:
                stats['pass2: row vanished'] += 1
                continue
            try:
                _insert_row(metas[key], row, creator)
            except Exception as exc:  # noqa: BLE001
                stats['DB error'] += 1
                print(f'  ERROR {key}: {exc}')
                continue
            stats[f'imported:{metas[key]["platform"]}'] += 1
            imported += 1
            if imported % PROGRESS_EVERY == 0:
                print(f'  imported {imported}/{len(planned)} '
                      f'({time.monotonic() - started:.0f}s)', flush=True)
        del materialized
        gc.collect()
    return stats


# ---- CLI ------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--user', default='oscar')
    parser.add_argument('--limit', type=int, default=0,
                        help='import at most N (0 = derive from target / all)')
    parser.add_argument('--target-total', type=int, default=0,
                        help='stop when site has this many problems (0 = no cap)')
    parser.add_argument('--min-tests', type=int, default=1)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--skip-platforms', default='',
                        help='comma-separated platform names to skip '
                             '(e.g. "codeforces,atcoder")')
    args = parser.parse_args()

    User = get_user_model()
    if not User.objects.filter(username=args.user).exists():
        print(f'ERROR: user "{args.user}" does not exist.', file=sys.stderr)
        return 1
    if not shard_paths():
        print(f'ERROR: no parquet shards in {CACHE_DIR}; see script header.',
              file=sys.stderr)
        return 1

    skip_platforms = {p.strip().lower() for p in args.skip_platforms.split(',')
                      if p.strip()}

    metas, stats = scan_light(args.min_tests)
    print('\n===== candidates by platform =====')
    for p in sorted({m['platform'] for m in metas.values()}):
        n = sum(1 for m in metas.values() if m['platform'] == p)
        print(f'  {platform_label(p):16s} {n}')

    plan = select_for_import(metas, stats, skip_platforms)
    stats['dup: total removed'] = len(metas) - len(plan)

    print('\n===== net-new by platform (after dedup) =====')
    per_platform = Counter(metas[k]['platform'] for k in plan)
    for p, n in sorted(per_platform.items(), key=lambda kv: -kv[1]):
        print(f'  {platform_label(p):16s} {n}')

    print('\n===== filter stats =====')
    for reason, n in sorted(stats.items()):
        print(f'  {n:>6}  {reason}')

    if args.dry_run:
        print('\nmode: DRY RUN | no DB writes')
        return 0

    if args.limit:
        quota = min(args.limit, len(plan))
    elif args.target_total:
        quota = min(max(args.target_total - Problem.objects.count(), 0),
                    len(plan))
    else:
        quota = len(plan)
    print(f'\nquota to import: {quota}')
    if quota <= 0:
        print('nothing to do.')
        return 0

    istats = run_import(metas, plan, quota, args.user)
    print('\n========== summary ==========')
    for reason, n in sorted(istats.items()):
        print(f'  {n:>6}  {reason}')
    print(f'total now: {Problem.objects.count()}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
