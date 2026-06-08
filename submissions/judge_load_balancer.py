"""Load balancer for distributing judge tasks across multiple judge machines."""

import logging
import random
from django.conf import settings
from django.core.cache import cache
from django_redis import get_redis_connection

logger = logging.getLogger(__name__)


class JudgeLoadBalancer:
    """Load balancer for distributing judge tasks across multiple judge machines."""

    def __init__(self):
        self.machines = getattr(settings, 'JUDGE_MACHINES', [])
        self.multi_judge_enabled = getattr(settings, 'OJ_MULTI_JUDGE_ENABLED', False)
        self.health_check_cache_prefix = 'judge_health_'
        self.health_check_ttl = 30  # Health check results valid for 30 seconds

    def get_enabled_machines(self):
        """Get list of enabled judge machines."""
        if not self.multi_judge_enabled:
            return []
        return [m for m in self.machines if m.get('enabled', True)]

    def check_machine_health(self, machine):
        """Check if a judge machine is healthy."""
        cache_key = f'{self.health_check_cache_prefix}{machine["name"]}'
        
        # Check cache first
        cached_health = cache.get(cache_key)
        if cached_health is not None:
            return cached_health

        # Perform health check by connecting to Redis
        try:
            redis_conn = get_redis_connection(
                # f'judge-{machine["name"]}',
                # machine['queue'],
                'default',
                {
                    'HOST': machine['host'],
                    'PORT': machine['port'],
                    'DB': machine['db'],
                    'OPTIONS': {
                        'CLIENT_CLASS': 'django_redis.client.DefaultClient'
                    }
                }
            )
            redis_conn.ping()
            is_healthy = True
            logger.info(f'Judge machine {machine["name"]} is healthy')
        except Exception as e:
            is_healthy = False
            logger.warning(f'Judge machine {machine["name"]} health check failed: {e}')

        # Cache the result
        cache.set(cache_key, is_healthy, self.health_check_ttl)
        return is_healthy

    def get_healthy_machines(self):
        """Get list of healthy judge machines."""
        enabled_machines = self.get_enabled_machines()
        healthy_machines = []
        
        for machine in enabled_machines:
            if self.check_machine_health(machine):
                healthy_machines.append(machine)
        
        return healthy_machines

    def select_machine(self):
        """Select a judge machine using weighted round-robin."""
        if not self.multi_judge_enabled:
            return None

        healthy_machines = self.get_healthy_machines()
        
        if not healthy_machines:
            logger.warning('No healthy judge machines available, falling back to default queue')
            return None

        # Weighted random selection
        total_weight = sum(m.get('weight', 1) for m in healthy_machines)
        if total_weight == 0:
            return random.choice(healthy_machines)

        rand = random.uniform(0, total_weight)
        current_weight = 0
        
        for machine in healthy_machines:
            current_weight += machine.get('weight', 1)
            if rand <= current_weight:
                logger.info(f'Selected judge machine: {machine["name"]}')
                return machine

        return healthy_machines[-1]  # Fallback to last machine

    def get_queue_for_machine(self, machine):
        """Get RQ queue configuration for a specific judge machine."""
        if not machine:
            return 'default'

        return {
            'name': machine['queue'],
            'connection_config': {
                'HOST': machine['host'],
                'PORT': machine['port'],
                'DB': machine['db'],
                'DEFAULT_TIMEOUT': 3600,
                'WORKER_CLASS': 'rq.Worker',
            }
        }


# Global load balancer instance
load_balancer = JudgeLoadBalancer()
