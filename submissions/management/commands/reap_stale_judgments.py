"""Reclaim dead judge claims and requeue them.

Two kinds of stuck submissions are recovered:

1. ``JUDGING`` rows whose heartbeat lease expired (worker crash / kill -9 /
   network partition) — the claim is atomically cleared and the job is
   reposted to the (central or legacy, per the current flag) broker.
2. ``QUEUED`` rows that no worker ever claimed within the queued timeout
   (dropped broker message) — simply reposted.

Run once (cron / systemd timer)::

    python manage.py reap_stale_judgments

Or as a long-running loop (systemd service)::

    python manage.py reap_stale_judgments --loop --interval 30
"""

import logging
import time

from django.core.management.base import BaseCommand

logger = logging.getLogger('submissions.reaper')


class Command(BaseCommand):
    help = 'Requeue stale judge claims (expired heartbeats / lost queued jobs).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--judging-timeout', type=int, default=300,
            help='Seconds without a heartbeat before a JUDGING claim is reaped.',
        )
        parser.add_argument(
            '--queued-timeout', type=int, default=600,
            help='Seconds a QUEUED row may sit unclaimed before reposting.',
        )
        parser.add_argument(
            '--limit', type=int, default=200,
            help='Max submissions reclaimed per pass.',
        )
        parser.add_argument(
            '--loop', action='store_true',
            help='Run forever instead of a single pass.',
        )
        parser.add_argument(
            '--interval', type=int, default=30,
            help='Seconds between passes in --loop mode.',
        )

    def handle(self, *args, **options):
        from submissions.claiming import reap_stale_claims
        from submissions.judge_queue import enqueue_judge
        from submissions.realtime import publish_submission_changed

        judging_timeout = options['judging_timeout']
        queued_timeout = options['queued_timeout']
        limit = options['limit']

        def pass_once():
            requeued = reap_stale_claims(
                judging_timeout_secs=judging_timeout,
                queued_timeout_secs=queued_timeout,
                limit=limit,
            )
            for sid in requeued:
                try:
                    enqueue_judge(sid)
                    publish_submission_changed(sid)
                except Exception:
                    logger.exception(
                        'Reaper failed to re-enqueue submission %s', sid,
                    )
            return len(requeued)

        if not options['loop']:
            count = pass_once()
            self.stdout.write(f'Requeued {count} stale submission(s).\n')
            return

        self.stdout.write(
            f'Reaper loop started (interval={options["interval"]}s, '
            f'judging_timeout={judging_timeout}s, queued_timeout={queued_timeout}s).\n'
        )
        while True:
            try:
                count = pass_once()
                if count:
                    self.stdout.write(
                        f'{time.strftime("%Y-%m-%d %H:%M:%S")} '
                        f'requeued {count} stale submission(s).\n'
                    )
            except Exception:
                logger.exception('Reaper pass failed')
            time.sleep(options['interval'])
