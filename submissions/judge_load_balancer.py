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
        """Check if a judge machine is healthy by connecting to its Redis instance."""
        cache_key = f'{self.health_check_cache_prefix}{machine["name"]}'
        
        # Check cache first
        cached_health = cache.get(cache_key)
        if cached_health is not None:
            return cached_health

        # Perform health check by connecting to Redis
        try:
            import redis
            redis_conn = redis.Redis(
                host=machine['host'],
                port=machine['port'],
                db=machine['db'],
                socket_connect_timeout=5,
                socket_timeout=5,
                decode_responses=True
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

    def _get_queue_length(self, machine):
        """Get number of pending/enqueued jobs on this machine's queue.
        Returns 0 if the queue can't be reached (treat as overloaded to avoid)."""
        try:
            import redis
            r = redis.Redis(
                host=machine['host'],
                port=machine['port'],
                db=machine['db'],
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            # RQ stores jobs under rq:queue:<name>
            return r.llen(f"rq:queue:{machine['queue']}")
        except Exception:
            # Can't reach Redis — treat as heavily loaded so we skip it
            return 9999

    def select_machine(self):
        """Select the least-loaded healthy judge machine.
        Machines with fewer pending jobs are preferred.
        Among equally-loaded machines, weight is used for tie-breaking."""
        if not self.multi_judge_enabled:
            return None

        healthy_machines = self.get_healthy_machines()
        if not healthy_machines:
            logger.warning('No healthy judge machines available, falling back to default queue')
            return None

        # Query queue lengths for all healthy machines
        machine_loads = []
        for m in healthy_machines:
            qlen = self._get_queue_length(m)
            machine_loads.append((qlen, m))
            logger.debug(f'  {m["name"]}: {qlen} pending jobs')

        # Sort by queue length (ascending — less loaded first)
        machine_loads.sort(key=lambda x: x[0])

        # Group machines with the same (minimum) queue length
        min_load = machine_loads[0][0]
        candidates = [m for qlen, m in machine_loads if qlen == min_load]

        # If only one candidate, return it
        if len(candidates) == 1:
            logger.info(f'Selected judge machine: {candidates[0]["name"]} '
                        f'(load: {min_load}, only idle candidate)')
            return candidates[0]

        # Tie-break among equally-loaded machines using weighted random
        total_weight = sum(m.get('weight', 1) for m in candidates)
        rand = random.uniform(0, total_weight)
        current = 0
        for m in candidates:
            current += m.get('weight', 1)
            if rand <= current:
                logger.info(f'Selected judge machine: {m["name"]} '
                            f'(load: {min_load}, weighted among {len(candidates)} candidates)')
                return m

        return candidates[-1]

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
                'WORKER_CLASS': 'oj_project.customrq.AutoReconnectWorker',
            }
        }


# Global load balancer instance
load_balancer = JudgeLoadBalancer()
