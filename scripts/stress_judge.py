#!/usr/bin/env python
"""Judge-system stress test.

Submits a batch of solutions straight through the ORM + Celery judge queue
(bypassing web views / captchas / rate limits) against a live judge worker,
then polls until every submission reaches a terminal status and reports
latency percentiles and throughput.

Usage (from the project root, with the project venv):

    venv/bin/python scripts/stress_judge.py --count 20 --yes
    venv/bin/python scripts/stress_judge.py --count 50 --language Python --ramp 10
    venv/bin/python scripts/stress_judge.py --count 100 --workers 16 --yes
    venv/bin/python scripts/stress_judge.py --problem 123 --count 30 --code-file ac.cpp
    venv/bin/python scripts/stress_judge.py --count 10 --workers 1 --keep  # serial, keep rows

Behaviour:
* Submissions are attributed to a dedicated, non-loginable stress account
  (``__stress_bot__``, ``is_active=False``), mirroring the ``__ai_judge_bot__``
  convention, so real user stats / leaderboards are untouched.
* Problem selection: ``--problem <id>`` or the problem with the FEWEST test
  cases (cheapest to judge).
* Default payload is an echo program: it exercises the full
  compile -> run -> compare path and is expected to finish as Wrong Answer
  after the first case.  Pass ``--code-file`` with a correct solution to
  measure the full all-cases path instead.
* Rows are deleted again on exit (CASCADE removes test results) unless
  ``--keep`` is given.
* Submissions are created + enqueued from a thread pool (``--workers``,
  default 8); every worker uses its own per-thread DB connection.
"""
from __future__ import annotations

import argparse
import os
import random
import statistics
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

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import close_old_connections  # noqa: E402
from django.db.models import Count  # noqa: E402

from problems.models import Problem  # noqa: E402
from submissions.judge import JUDGED_LANGUAGES  # noqa: E402
from submissions.judge_queue import enqueue_judge  # noqa: E402
from submissions.models import Submission  # noqa: E402

User = get_user_model()

STRESS_USERNAME = '__stress_bot__'

# Terminal statuses a submission can settle into.
TERMINAL_STATUSES = {
    'Accepted', 'Wrong Answer', 'Time Limit Exceeded',
    'Memory Limit Exceeded', 'Runtime Error', 'Compile Error',
    'System Error',
}

# Trivial echo program: full compile+run+compare path, WA expected.
DEFAULT_CODE = (
    '#include <bits/stdc++.h>\n'
    'int main(){std::cout<<std::cin.rdbuf();return 0;}\n'
)


def percentile(values, pct):
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, round(len(ordered) * pct / 100))
    return ordered[idx]


def pick_problem(problem_id):
    if problem_id is not None:
        problem = Problem.objects.filter(pk=problem_id).first()
        if problem is None:
            sys.exit(f'错误：题目 {problem_id} 不存在。')
        if not problem.test_cases.exists():
            sys.exit(f'错误：题目 {problem_id} 没有测试点，无法评测。')
        return problem
    # Cheapest target: the problem with the fewest test cases.
    return (
        Problem.objects
        .annotate(tc=Count('test_cases'))
        .filter(tc__gt=0)
        .order_by('tc', 'id')
        .first()
    )


def get_stress_user(username):
    user, created = User.objects.get_or_create(
        username=username,
        defaults={
            'email': f'{username}@stress.invalid',
            'is_active': False,   # cannot log in, same as __ai_judge_bot__
        },
    )
    if user.is_active:
        # Refuse to hijack a real account.
        sys.exit(f'错误：用户 {username} 已存在且处于激活状态，拒绝借用真实账号压测。')
    return user


def _create_and_enqueue(user_id, problem_id, code, language, retries):
    """Thread worker: create one submission and enqueue judging.

    Runs on its own thread-local DB connection, closed before/after the
    job so connections are never shared or leaked (same convention as the
    judge worker's per-job ``close_old_connections``).

    Machine reservation is lock-free, but the Redis probes and enqueue can
    still hit transient hiccups under a burst. Retry with jittered backoff
    to ride those out; give up after ``retries`` attempts and report the
    failure.

    Returns dict {id, submitted_at, error, attempts}.
    """
    close_old_connections()
    try:
        submission = Submission.objects.create(
            problem_id=problem_id,
            user_id=user_id,
            code=code,
            language=language,
            status='Pending',
        )
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                enqueue_judge(submission.id)
                return {
                    'id': submission.id,
                    'submitted_at': time.monotonic(),
                    'error': None,
                    'attempts': attempt,
                }
            except Exception as exc:  # Redis / enqueue hiccup
                last_error = exc
                if attempt < retries:
                    # 0.2s, 0.4s, 0.8s ... + jitter
                    time.sleep(0.2 * (2 ** (attempt - 1))
                             + random.random() * 0.1)
        return {
            'id': submission.id,
            'submitted_at': time.monotonic(),
            'error': f'{type(last_error).__name__}: {last_error}',
            'attempts': retries,
        }
    finally:
        close_old_connections()


def submit_batch(user, problem, code, language, count, ramp, workers, retries):
    """Create + enqueue ``count`` submissions in parallel.

    Returns (entries, enqueue_failures): entries in completion order, each
    {id, submitted_at, error, attempts}; enqueue_failures is the subset
    whose retries were exhausted.
    """
    entries = []
    failures = []
    interval = (ramp / count) if (ramp and count > 1) else 0.0
    mode = f'{workers} 线程并行' if workers > 1 else '单线程串行'
    print(f'开始提交 {count} 份 {language} 代码（{mode}'
          + (f'，线性爬坡 {ramp}s）' if interval else '）'))
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = []
        for _ in range(count):
            futures.append(pool.submit(
                _create_and_enqueue, user.id, problem.id, code, language, retries,
            ))
            # Stagger dispatch for the ramp; without ramp the whole batch is
            # dispatched immediately and limited only by the pool size.
            if interval:
                time.sleep(interval)
        for future in as_completed(futures):
            entry = future.result()  # raises on unexpected worker failure
            entries.append(entry)
            if entry['error']:
                failures.append(entry)
            done += 1
            if done % 10 == 0 or done == count:
                extra = f'，入队失败 {len(failures)}' if failures else ''
                print(f'  已提交 {done}/{count}{extra}')
    return entries, failures


def poll_until_done(entries, poll_interval, timeout):
    """Poll statuses until all terminal / timeout / Ctrl+C.

    Returns (finish_times: {id: elapsed}, remaining_ids).
    """
    pending_ids = [e['id'] for e in entries]
    finish_times = {}
    deadline = time.monotonic() + timeout
    last_pending_count = -1

    print(f'\n轮询评测结果（间隔 {poll_interval}s，超时 {timeout}s）...')
    try:
        while pending_ids and time.monotonic() < deadline:
            # time.sleep(poll_interval)
            rows = Submission.objects.filter(
                id__in=pending_ids,
            ).values_list('id', 'status')
            now = time.monotonic()
            still_pending = []
            for sid, status in rows:
                if status in TERMINAL_STATUSES:
                    finish_times[sid] = now
                else:
                    still_pending.append(sid)
            pending_ids = still_pending
            if len(pending_ids) != last_pending_count:
                # print(f'  [{time.strftime("%H:%M:%S")}] '
                    #   f'已完成 {len(entries) - len(pending_ids)}/{len(entries)}，'
                    #   f'待评测 {len(pending_ids)}')
                last_pending_count = len(pending_ids)
    except KeyboardInterrupt:
        print('\n收到中断，停止轮询。')

    return finish_times, pending_ids


def report(entries, finish_times, problem, enqueue_failures):
    submitted_at = {e['id']: e['submitted_at'] for e in entries}
    finished_ids = [sid for sid in submitted_at if sid in finish_times]

    statuses = dict(
        Submission.objects.filter(id__in=submitted_at)
        .values_list('id', 'status')
    )
    dist = Counter(statuses.values())

    print('\n========== 压测报告 ==========')
    print(f'题目:          #{problem.id} {problem.title}')
    print(f'提交总数:      {len(entries)}')
    if enqueue_failures:
        print(f'入队失败:      {len(enqueue_failures)}'
              '（负载均衡器预留容量超时，见下方明细）')
    print(f'已出结果:      {len(finished_ids)}')
    print(f'评测未完成:    {len(entries) - len(finished_ids) - len(enqueue_failures)}'
          + ('（可能是 worker 未运行或积压，检查 systemctl status guwu-oj-judge-worker）'
             if len(finished_ids) + len(enqueue_failures) < len(entries) else ''))

    print('\n结果分布:')
    for status, cnt in dist.most_common():
        label = status
        if status == 'Pending' and enqueue_failures:
            label = 'Pending（含入队失败）'
        print(f'  {label:<26} {cnt}')

    system_errors = dist.get('System Error', 0)
    if system_errors:
        print(f'\n警告: 出现 {system_errors} 次 System Error，评测系统可能已经过载。')
    if enqueue_failures:
        attempts = sorted(e['attempts'] for e in enqueue_failures)
        print(f'\n警告: {len(enqueue_failures)} 份提交在 {attempts[0]}~{attempts[-1]} '
              '次重试后仍无法入队，全局选择锁（5s 等待）可能是瓶颈。')
        for entry in enqueue_failures[:3]:
            print(f'  提交 {entry["id"]}: {entry["error"]}')
        if len(enqueue_failures) > 3:
            print(f'  … 另有 {len(enqueue_failures) - 3} 条')

    if finished_ids:
        latencies = [
            finish_times[sid] - submitted_at[sid] for sid in finished_ids
        ]
        wall = max(finish_times.values()) - min(submitted_at.values())
        print('\n端到端延迟（提交 -> 终态，含排队）:')
        print(f'  平均  {statistics.mean(latencies):8.1f}s')
        print(f'  p50   {percentile(latencies, 50):8.1f}s')
        print(f'  p90   {percentile(latencies, 90):8.1f}s')
        print(f'  p99   {percentile(latencies, 99):8.1f}s')
        print(f'  最大  {max(latencies):8.1f}s')
        print(f'\n吞吐: {len(finished_ids) / wall:.2f} 份/秒'
              f'（总观测窗口 {wall:.1f}s，受轮询粒度影响）')


def cleanup(ids, keep):
    if not ids:
        return
    if keep:
        print(f'\n按要求保留 {len(ids)} 条提交记录（ID: {ids[0]}..{ids[-1]}）。')
        return
    deleted, _ = Submission.objects.filter(id__in=ids).delete()
    print(f'\n已清理压测数据：删除 {deleted} 行（含级联的测试点结果）。')


def main():
    parser = argparse.ArgumentParser(description='评测系统压测')
    parser.add_argument('--count', type=int, default=20, help='提交份数（默认 20）')
    parser.add_argument('--language', default='C++', choices=sorted(JUDGED_LANGUAGES),
                        help='评测语言（默认 C++）')
    parser.add_argument('--problem', type=int, default=None,
                        help='目标题目 ID（默认自动选测试点最少的题）')
    parser.add_argument('--code-file', default=None,
                        help='提交的代码文件（默认回显程序，预期 WA）')
    parser.add_argument('--user', default=STRESS_USERNAME,
                        help=f'压测用户名（默认 {STRESS_USERNAME}）')
    parser.add_argument('--workers', type=int, default=8,
                        help='并行提交线程数（默认 8，1 = 串行）')
    parser.add_argument('--enqueue-retries', type=int, default=5,
                        help='入队（选择锁）冲突时的重试次数（默认 5）')
    parser.add_argument('--ramp', type=float, default=0.0,
                        help='线性爬坡时长（秒），0 = 瞬时全部提交')
    parser.add_argument('--timeout', type=int, default=600,
                        help='整体轮询超时秒数（默认 600）')
    parser.add_argument('--poll-interval', type=float, default=0.2,
                        help='轮询间隔秒数（默认 2）')
    parser.add_argument('--keep', action='store_true',
                        help='保留提交记录（默认退出前删除）')
    parser.add_argument('--yes', action='store_true', help='跳过确认直接执行')
    args = parser.parse_args()

    if args.count < 1 or args.count > 1000:
        sys.exit('错误：--count 须在 1..1000 之间。')
    if args.ramp < 0:
        sys.exit('错误：--ramp 不能为负。')
    if args.workers < 1 or args.workers > 64:
        sys.exit('错误：--workers 须在 1..64 之间。')
    if args.enqueue_retries < 1 or args.enqueue_retries > 20:
        sys.exit('错误：--enqueue-retries 须在 1..20 之间。')

    problem = pick_problem(args.problem)
    if problem is None:
        sys.exit('错误：题库中没有任何带测试点的题目。')

    test_count = problem.test_cases.count()
    code = DEFAULT_CODE
    if args.code_file:
        code = Path(args.code_file).read_text(encoding='utf-8')

    user = get_stress_user(args.user)

    print('========== 压测计划 ==========')
    print(f'目标:          #{problem.id} {problem.title}（{test_count} 个测试点）')
    print(f'数量/语言:     {args.count} 份 / {args.language}')
    print(f'提交并发:      {args.workers} 线程' + ('（串行）' if args.workers == 1 else '')
          + f'，入队重试 {args.enqueue_retries} 次')
    print(f'代码来源:      {"文件 " + args.code_file if args.code_file else "内置回显程序（预期 WA）"}')
    print(f'压测账号:      {user.username}（is_active=False）')
    print(f'结果保留:      {"是" if args.keep else "否（退出前删除）"}')

    if not args.yes:
        answer = input('\n确认执行？[y/N] ').strip().lower()
        if answer not in ('y', 'yes'):
            print('已取消。')
            return

    t0 = time.monotonic()
    entries, enqueue_failures = submit_batch(
        user, problem, code, args.language, args.count, args.ramp,
        args.workers, args.enqueue_retries,
    )
    print(f'提交阶段耗时 {time.monotonic() - t0:.1f}s')

    # Only successfully enqueued rows can reach a terminal status.
    enqueued = [e for e in entries if not e['error']]
    finish_times, still_pending = poll_until_done(
        enqueued, args.poll_interval, args.timeout,
    )
    report(entries, finish_times, problem, enqueue_failures)
    cleanup([e['id'] for e in entries], args.keep)

    if still_pending and not args.keep:
        print(f'注意: {len(still_pending)} 条仍为 Pending 的记录已被一并删除。')


if __name__ == '__main__':
    main()
