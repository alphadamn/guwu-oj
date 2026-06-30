"""Load balancer for distributing judge tasks across multiple judge machines."""

import logging
import os
import random
from django.conf import settings
from django.core.cache import cache
from django_redis import get_redis_connection

logger = logging.getLogger(__name__)


class JudgeLoadBalancer:
    """Load balancer for distributing judge tasks across multiple judge machines."""

    def __init__(self):
        self.multi_judge_enabled = getattr(settings, 'OJ_MULTI_JUDGE_ENABLED', False)
        self.health_check_cache_prefix = 'judge_health_'
        self.health_check_ttl = 30

    @property
    def machines(self):
        """Get machines from DB, fallback to settings.py."""
        from submissions.models import JudgeMachine
        try:
            db_machines = list(JudgeMachine.objects.values(
                'name', 'host', 'port', 'db', 'queue', 'enabled', 'weight'
            ))
            if db_machines:
                return db_machines
        except Exception:
            pass
        return getattr(settings, 'JUDGE_MACHINES', [])

    def _machine_redis(self, machine, decode_responses=False):
        from django.conf import settings
        kwargs = {
            'host': machine['host'],
            'port': machine['port'],
            'db': machine['db'],
            'socket_connect_timeout': 3,
            'socket_timeout': 3,
            'decode_responses': decode_responses,
        }
        password = os.environ.get('RQ_REDIS_PASSWORD', '')
        if password:
            kwargs['password'] = password
        return redis.Redis(**kwargs)

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
        """Get number of pending/enqueued jobs on this machine's queue."""
        try:
            import redis
            r = redis.Redis(
                host=machine['host'],
                port=machine['port'],
                db=machine['db'],
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            return r.llen(f"rq:queue:{machine['queue']}")
        except Exception:
            return 9999

    def _get_busy_count(self, machine):
        """Get number of jobs currently being executed by this machine's worker."""
        try:
            import redis
            r = redis.Redis(
                host=machine['host'],
                port=machine['port'],
                db=machine['db'],
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            return int(r.get(f"judge:busy:{machine['name']}") or 0)
        except Exception:
            return 9999

    def _incr_busy(self, machine):
        """Increment busy count when a job is dispatched to this machine."""
        try:
            import redis
            r = redis.Redis(
                host=machine['host'],
                port=machine['port'],
                db=machine['db'],
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            r.incr(f"judge:busy:{machine['name']}")
            r.expire(f"judge:busy:{machine['name']}", 3600)
        except Exception:
            pass

    def _decr_busy(self, machine):
        """Decrement busy count when a job finishes on this machine."""
        try:
            import redis
            r = redis.Redis(
                host=machine['host'],
                port=machine['port'],
                db=machine['db'],
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            val = r.decr(f"judge:busy:{machine['name']}")
            print(val, f"judge:busy:{machine['name']}")
            if val <= 0:
                r.delete(f"judge:busy:{machine['name']}")
        except Exception:
            pass

    def _set_submission_machine(self, submission_id, machine_name):
        """Record which machine is processing a submission."""
        try:
            cache.set(f'judge:sub_machine:{submission_id}', machine_name, 3600)
        except Exception:
            pass

    def _get_and_clear_submission_machine(self, submission_id):
        """Get and clear the machine assignment for a submission."""
        try:
            key = f'judge:sub_machine:{submission_id}'
            machine_name = cache.get(key)
            print(machine_name)
            if machine_name:
                cache.delete(key)
            return machine_name
        except Exception:
            return None

    def release_machine(self, submission_id):
        """Called by the task when judging completes to decrement busy count."""
        machine_name = self._get_and_clear_submission_machine(submission_id)
        if machine_name:
            # Find the machine config to get host/port/db
            for m in self.machines:
                if m['name'] == machine_name:
                    self._decr_busy(m)
                    logger.debug(f'Released machine {machine_name} for submission {submission_id}')
                    return

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

        # Query queue lengths AND busy counts for all healthy machines
        machine_loads = []
        for m in healthy_machines:
            qlen = self._get_queue_length(m)
            busy = self._get_busy_count(m)
            total_load = qlen + busy  # pending + currently executing
            machine_loads.append((total_load, m))
            logger.debug(f'  {m["name"]}: queue={qlen}, busy={busy}, total={total_load}')

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
