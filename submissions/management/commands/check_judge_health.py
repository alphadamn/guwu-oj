"""Django management command to check health of judge machines."""

from django.core.management.base import BaseCommand
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
        
        for machine in machines:
            is_healthy = load_balancer.check_machine_health(machine)
            status = 'HEALTHY' if is_healthy else 'UNHEALTHY'
            
            if is_healthy:
                self.stdout.write(self.style.SUCCESS(f'✓ {machine["name"]}: {status}'))
                self.stdout.write(f'  Host: {machine["host"]}:{machine["port"]}')
                self.stdout.write(f'  Queue: {machine["queue"]}')
                self.stdout.write(f'  Weight: {machine.get("weight", 1)}')
                healthy_count += 1
            else:
                self.stdout.write(self.style.ERROR(f'✗ {machine["name"]}: {status}'))
                self.stdout.write(f'  Host: {machine["host"]}:{machine["port"]}')
                self.stdout.write(f'  Queue: {machine["queue"]}')
                unhealthy_count += 1
            
            self.stdout.write('')
        
        self.stdout.write(f'\nSummary: {healthy_count} healthy, {unhealthy_count} unhealthy')
        
        if unhealthy_count > 0:
            self.stdout.write(self.style.WARNING('\nSome judge machines are unhealthy. Check Redis connectivity and configuration.'))
