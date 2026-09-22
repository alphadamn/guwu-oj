"""Rebuild the direct judge-API firewall chain from the reported-IP state.

The chain (``JUDGE_DIRECT``) is default-deny and owns
``--dport $OJ_JUDGE_DIRECT_PORT``, so it must exist even before any worker
reports: iptables rules do not survive a reboot, and the static entries in
``OJ_JUDGE_DIRECT_STATIC_IPS`` are applied only by this command.

Run once (boot / cron)::

    python manage.py sync_judge_firewall

Or as a long-running loop (systemd service)::

    python manage.py sync_judge_firewall --loop --interval 300
"""

import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger('submissions.judge_firewall')


class Command(BaseCommand):
    help = 'Sync the direct judge API iptables chain with reported worker IPs.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--loop', action='store_true',
            help='Run forever instead of a single pass.',
        )
        parser.add_argument(
            '--interval', type=int, default=300,
            help='Seconds between passes in --loop mode.',
        )

    def handle(self, *args, **options):
        from submissions.judge_firewall import apply_firewall

        loud = options['verbosity'] > 1

        def pass_once():
            sources = apply_firewall()
            if sources is None:
                self.stderr.write(
                    'iptables unavailable; direct judge API firewall NOT synced.\n'
                )
                return None
            if loud:
                self.stdout.write(f'Allowed sources: {sources or "(none)"}\n')
            return sources

        state_path = getattr(
            settings, 'OJ_JUDGE_DIRECT_STATE', '',
        )
        port = getattr(settings, 'OJ_JUDGE_DIRECT_PORT', 8446)

        if not options['loop']:
            if pass_once() is not None:
                self.stdout.write(
                    f'Direct judge API :{port} chain synced '
                    f'(state: {state_path}).\n'
                )
            return

        self.stdout.write(
            f'Firewall sync loop started (interval={options["interval"]}s, '
            f'port={port}, state={state_path}).\n'
        )
        while True:
            try:
                sources = pass_once()
                if sources and loud:
                    self.stdout.write(
                        f'{time.strftime("%Y-%m-%d %H:%M:%S")} '
                        f'allowed {sources}\n'
                    )
            except Exception:
                logger.exception('Direct judge API firewall sync failed')
            time.sleep(options['interval'])
