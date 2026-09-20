"""Consume DB-less worker outcomes from the ``judge:result`` list.

Runs on the web host as a dedicated service (guwu-oj-judge-result-consumer).
Every envelope is persisted through the idempotent, fencing-protected
:func:`submissions.results.process_envelope`; see
:mod:`submissions.result_queue` for the crash/reliability semantics.

    python manage.py consume_judge_results            # blocking loop
    python manage.py consume_judge_results --once     # drain one message
"""

import logging
import time

from django.core.management.base import BaseCommand

logger = logging.getLogger('submissions.result_consumer')


class Command(BaseCommand):
    help = 'Consume judge outcomes from the judge:result Redis list.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--timeout', type=int, default=5,
            help='BRPOPLPUSH block timeout in seconds.',
        )
        parser.add_argument(
            '--once', action='store_true',
            help='Process a single message (or none) and exit.',
        )

    def handle(self, *args, **options):
        from django.conf import settings
        from django_rq import get_queue

        from submissions import result_queue
        from submissions.results import process_envelope

        # Same Redis the central judge lanes live on; workers push results
        # onto the connection their job arrived on.
        conn = get_queue(
            getattr(settings, 'OJ_CENTRAL_QUEUE_NAME', 'judge:queue')
        ).connection

        recovered = result_queue.recover_processing(conn)
        if recovered:
            self.stdout.write(f'Recovered {recovered} in-flight message(s).\n')

        self.stdout.write('Result consumer started.\n')
        while True:
            try:
                raw = result_queue.fetch_for_processing(
                    conn, timeout_secs=options['timeout'],
                )
                if raw is None:
                    if options['once']:
                        return
                    continue
                try:
                    outcome = process_envelope(raw, conn)
                    result_queue.ack(conn, raw)
                    logger.debug('processed envelope -> %s', outcome)
                except Exception as exc:
                    # Poison / persistent failure: park it and stay alive.
                    logger.exception('Failed processing result envelope')
                    result_queue.dead_letter(conn, raw, exc)
                if options['once']:
                    return
            except KeyboardInterrupt:
                self.stdout.write('Stopping.\n')
                return
            except Exception:
                logger.exception('Consumer loop error; backing off 2s')
                time.sleep(2)
