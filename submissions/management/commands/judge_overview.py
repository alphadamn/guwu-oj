"""Judge fleet observability snapshot.

Prints queue depths per Celery priority bucket, online workers (broker
heartbeat keys), lifecycle-state counts, stuck claims, recent latency
percentiles and the System Error rate. Read-only; safe to run any time.

    python manage.py judge_overview
"""

import statistics
import time
from collections import Counter

from django.db.models import Avg, Count, F
from django.utils import timezone

from django.core.management.base import BaseCommand

from submissions.judge_queue import (
    PRIORITY_AI,
    PRIORITY_DEFAULT,
    PRIORITY_PLUS,
    PRIORITY_PRO,
    celery_priority,
)
from submissions.models import Submission
from submissions.result_queue import broker_client


def _bucket_list_name(queue, priority):
    """Physical Redis list for a Celery priority bucket (kombu ``sep``)."""
    return queue if priority == 0 else f'{queue}:{priority}'


class Command(BaseCommand):
    help = 'Print a judge fleet snapshot: queues, workers, states, latency.'

    def handle(self, *args, **options):
        conn = broker_client()
        now = timezone.now()

        # ── Queues ──────────────────────────────────────────────────────
        from django.conf import settings
        base = getattr(settings, 'CELERY_TASK_DEFAULT_QUEUE', 'judge')
        lanes = [
            (PRIORITY_PRO, celery_priority(PRIORITY_PRO)),
            (PRIORITY_PLUS, celery_priority(PRIORITY_PLUS)),
            (PRIORITY_DEFAULT, celery_priority(PRIORITY_DEFAULT)),
            (PRIORITY_AI, celery_priority(PRIORITY_AI)),
        ]
        self.stdout.write('── Queues (central broker, Celery priority buckets) ──')
        total_depth = 0
        for label, pri in lanes:
            name = _bucket_list_name(base, pri)
            depth = conn.llen(name)
            total_depth += depth
            self.stdout.write(f'  {name:12s} {label:8s} depth={depth}')
        self.stdout.write(f'  total pending jobs: {total_depth}')

        # ── Workers (broker heartbeat keys written by celery_worker) ────
        self.stdout.write('\n── Workers ──')
        online = 0
        for key in conn.scan_iter(match='judge:worker:*'):
            raw = conn.get(key)
            try:
                age = time.time() - float(raw)
            except (TypeError, ValueError):
                continue
            fresh = age <= 90
            online += 1 if fresh else 0
            name = key.decode() if isinstance(key, bytes) else key
            self.stdout.write(
                f'  {name:40s} last_beat={age:6.0f}s ago '
                f'{"(online)" if fresh else "(STALE)"}'
            )
        self.stdout.write(f'  online: {online}')

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
