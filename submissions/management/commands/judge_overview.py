"""Judge fleet observability snapshot.

Prints queue depths per central lane, online workers, lifecycle-state
counts, stuck claims, recent latency percentiles and the System Error
rate. Read-only; safe to run any time.

    python manage.py judge_overview
"""

import statistics
from collections import Counter

from django.db.models import Avg, Count, F
from django.utils import timezone

from django_rq import get_connection
from rq import Queue, Worker

from django.core.management.base import BaseCommand

from submissions.judge_queue import (
    PRIORITY_AI,
    PRIORITY_DEFAULT,
    PRIORITY_PLUS,
    PRIORITY_PRO,
)
from submissions.models import Submission


class Command(BaseCommand):
    help = 'Print a judge fleet snapshot: queues, workers, states, latency.'

    def handle(self, *args, **options):
        conn = get_connection()
        now = timezone.now()

        # ── Queues ──────────────────────────────────────────────────────
        from django.conf import settings
        base = getattr(settings, 'OJ_CENTRAL_QUEUE_NAME', 'judge:queue')
        lanes = [
            (PRIORITY_PRO, f'{base}-pro'),
            (PRIORITY_PLUS, f'{base}-plus'),
            (PRIORITY_DEFAULT, base),
            (PRIORITY_AI, f'{base}-ai'),
        ]
        self.stdout.write('── Queues (central broker) ──')
        total_depth = 0
        for label, name in lanes:
            depth = Queue(name, connection=conn).count
            total_depth += depth
            self.stdout.write(f'  {name:20s} {label:8s} depth={depth}')
        self.stdout.write(f'  total pending jobs: {total_depth}')

        # ── Workers ─────────────────────────────────────────────────────
        workers = Worker.all(connection=conn)
        self.stdout.write('\n── Workers ──')
        state_counter = Counter()
        for w in workers:
            state = w.get_state()
            state_counter[state] += 1
            current = ''
            job = w.get_current_job()
            if job is not None:
                current = f'  current={job.func_name}({job.args})'
            self.stdout.write(
                f'  {w.name[:12]:14s} state={state:7s} host={w.hostname}{current}'
            )
        self.stdout.write(
            f'  online: {len(workers)} ({dict(state_counter)})'
        )

        # ── Lifecycle states ────────────────────────────────────────────
        self.stdout.write('\n── Submission lifecycle (all time) ──')
        rows = dict(
            Submission.objects.values_list('judge_state')
            .annotate(c=Count('id'))
        )
        for state in ('PENDING', 'QUEUED', 'JUDGING', 'DONE', 'FAILED'):
            self.stdout.write(f'  {state:8s} {rows.get(state, 0)}')

        stuck = Submission.objects.filter(
            judge_state='JUDGING',
            heartbeat_at__lt=now - timezone.timedelta(seconds=300),
        ).count()
        queued_old = Submission.objects.filter(
            judge_state='QUEUED',
            created_at__lt=now - timezone.timedelta(seconds=600),
        ).count()
        self.stdout.write(
            f'\n  stale JUDGING (lease > 5min): {stuck}\n'
            f'  stale QUEUED (> 10min):        {queued_old}'
        )

        # ── Last-hour latency / failure rate ────────────────────────────
        since = now - timezone.timedelta(hours=1)
        recent = list(
            Submission.objects
            .filter(judge_state='DONE', finished_at__gte=since)
            .annotate(duration_ms=(
                (F('finished_at') - F('claimed_at'))
            ))
            .values_list('duration_ms', 'status')
        )
        self.stdout.write('\n── Last 1 hour (finished) ──')
        self.stdout.write(f'  judged: {len(recent)}')
        if recent:
            durations = sorted(
                d.total_seconds() for d, _ in recent if d is not None
            )
            if durations:
                def pct(p):
                    idx = min(len(durations) - 1,
                              round(len(durations) * p / 100))
                    return durations[idx]
                self.stdout.write(
                    f'  latency s: avg={statistics.mean(durations):.1f} '
                    f'p50={pct(50):.1f} p95={pct(95):.1f} max={durations[-1]:.1f}'
                )
            sys_errors = sum(1 for _, status in recent if status == 'System Error')
            self.stdout.write(
                f'  system errors: {sys_errors} '
                f'({sys_errors * 100 / len(recent):.1f}%)'
            )
