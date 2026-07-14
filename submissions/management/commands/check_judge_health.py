"""Django management command to check health of judge machines."""

from django.core.management.base import BaseCommand
from django.conf import settings

from submissions.judge_health import evaluate_machine_health
from submissions.judge_load_balancer import load_balancer


class Command(BaseCommand):
    help = 'Check health status of all configured judge machines'

    def handle(self, *args, **options):
        self.stdout.write('Checking judge machine health status...\n')

        machines = load_balancer.get_enabled_machines()
        if not machines:
            self.stdout.write(self.style.WARNING('No judge machines configured or multi-judge mode is disabled'))
            return

        self.stdout.write(f'Found {len(machines)} configured judge machine(s)\n')
        healthy_count = 0
        unhealthy_count = 0
        check_local_docker = getattr(settings, 'OJ_ROLE', 'web') == 'worker'

        for machine in machines:
            redis_client = load_balancer._machine_redis(machine, decode_responses=True)
            checks = evaluate_machine_health(machine, redis_client, check_local_docker=check_local_docker)
            is_healthy = checks['redis'][0] and checks['worker'][0]
            if check_local_docker:
                is_healthy = is_healthy and checks.get('docker', (True,))[0] and checks.get('images', (True,))[0]

            if is_healthy:
                self.stdout.write(self.style.SUCCESS(f'✓ {machine["name"]}: HEALTHY'))
                healthy_count += 1
            else:
                self.stdout.write(self.style.ERROR(f'✗ {machine["name"]}: UNHEALTHY'))
                unhealthy_count += 1

            self.stdout.write(f'  Host: {machine["host"]}:{machine["port"]}')
            self.stdout.write(f'  Queue: {machine["queue"]}')
            for name, (ok, detail) in checks.items():
                marker = 'ok' if ok else detail
                style = self.style.SUCCESS if ok else self.style.WARNING
                self.stdout.write(style(f'  {name}: {marker}'))
            self.stdout.write('')

        self.stdout.write(f'\nSummary: {healthy_count} healthy, {unhealthy_count} unhealthy')
        if unhealthy_count > 0:
            self.stdout.write(self.style.WARNING(
                '\nSome judge machines are unhealthy. Check Redis, worker heartbeat, Docker, and images.'
            ))
