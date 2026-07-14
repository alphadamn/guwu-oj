"""Load balancer for distributing judge tasks across multiple judge machines."""

import logging
import os
import random
from django.conf import settings

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
        import redis
        from django.conf import settings
        kwargs = {
            'host': machine['host'],
            'port': machine['port'],
            'db': machine['db'],
            'socket_connect_timeout': 3,
            'socket_timeout': 3,
            'decode_responses': decode_responses,
        }
        kwargs.update(getattr(settings, 'RQ_REDIS_CONNECTION_KWARGS', {}))
        return redis.Redis(**kwargs)

    def _find_machine(self, name=None, queue=None):
        for m in self.machines:
            if name and m.get('name') == name:
                return m
            if queue and m.get('queue') == queue:
                return m
        return None

    def get_enabled_machines(self):
        """Get list of enabled judge machines."""
        if not self.multi_judge_enabled:
            return []
        return [m for m in self.machines if m.get('enabled', True)]

    def check_machine_health(self, machine):
        """Check Redis, worker heartbeat, and optionally local Docker on workers."""
        from django.core.cache import cache
        from submissions.judge_health import evaluate_machine_health

        cache_key = f'{self.health_check_cache_prefix}{machine["name"]}'
        cached_health = cache.get(cache_key)
        if cached_health is not None:
            return cached_health

        redis_client = self._machine_redis(machine, decode_responses=True)
        check_local_docker = getattr(settings, 'OJ_ROLE', 'web') == 'worker'
        checks = evaluate_machine_health(machine, redis_client, check_local_docker=check_local_docker)

        is_healthy = checks['redis'][0] and checks['worker'][0]
        if check_local_docker:
            is_healthy = is_healthy and checks.get('docker', (True,))[0] and checks.get('images', (True,))[0]

        if is_healthy:
            logger.info('Judge machine %s is healthy', machine['name'])
        else:
            failed = {name: detail for name, (ok, detail) in checks.items() if not ok}
            logger.warning('Judge machine %s health check failed: %s', machine['name'], failed)

        cache.set(cache_key, is_healthy, self.health_check_ttl)
        return is_healthy

    def get_healthy_machines(self):
        """Get list of healthy judge machines."""
        enabled_machines = self.get_enabled_machines()
        return [m for m in enabled_machines if self.check_machine_health(m)]

    def _get_queue_length(self, machine):
        try:
            return self._machine_redis(machine).llen(f"rq:queue:{machine['queue']}")
        except Exception:
            return 9999

    def _get_busy_count(self, machine):
        try:
            return int(self._machine_redis(machine).get(f"judge:busy:{machine['name']}") or 0)
        except Exception:
            return 9999

    def _incr_busy(self, machine):
        try:
            r = self._machine_redis(machine)
            key = f"judge:busy:{machine['name']}"
            r.incr(key)
            r.expire(key, 3600)
        except Exception:
            logger.exception('Failed to increment busy count for %s', machine.get('name'))

    def _decr_busy(self, machine):
        try:
            r = self._machine_redis(machine)
            key = f"judge:busy:{machine['name']}"
            val = r.decr(key)
            if val <= 0:
                r.delete(key)
            logger.info('Decremented busy count for %s to %s', machine.get('name'), max(val, 0))
        except Exception:
            logger.exception('Failed to decrement busy count for %s', machine.get('name'))

    def _set_submission_machine(self, submission_id, machine_name):
        """Record which machine is processing a submission using Django cache."""
        try:
            from django.core.cache import cache
            cache.set(f'judge:sub_machine:{submission_id}', machine_name, 3600)
        except Exception:
            logger.exception(
                'Failed to record submission %s on machine %s',
                submission_id, machine_name,
            )

    def _get_and_clear_submission_machine(self, submission_id):
        """Get and clear the machine assignment for a submission using Django cache."""
        try:
            from django.core.cache import cache
            key = f'judge:sub_machine:{submission_id}'
            machine_name = cache.get(key)
            if machine_name:
                cache.delete(key)
            return machine_name
        except Exception:
            logger.exception('Failed to lookup submission machine for %s', submission_id)
        return None

    def release_machine(self, submission_id, queue_name=None):
        """Decrement busy count when judging completes."""
        machine_name = self._get_and_clear_submission_machine(submission_id)
        machine = self._find_machine(name=machine_name)

        if machine is None and queue_name:
            machine = self._find_machine(queue=queue_name)
            if machine:
                logger.info(
                    'Recovered machine %s for submission %s via queue %s',
                    machine['name'], submission_id, queue_name,
                )

        if machine is None:
            logger.warning(
                'Could not resolve judge machine for submission %s (queue=%s)',
                submission_id, queue_name,
            )
            return

        self._decr_busy(machine)

    def select_machine(self):
        if not self.multi_judge_enabled:
            return None

        healthy_machines = self.get_healthy_machines()
        if not healthy_machines:
            logger.warning('No healthy judge machines available, falling back to default queue')
            return None

        machine_loads = []
        for m in healthy_machines:
            qlen = self._get_queue_length(m)
            busy = self._get_busy_count(m)
            total_load = qlen + busy
            machine_loads.append((total_load, m))
            logger.debug(f'  {m["name"]}: queue={qlen}, busy={busy}, total={total_load}')

        machine_loads.sort(key=lambda x: x[0])
        min_load = machine_loads[0][0]
        candidates = [m for load, m in machine_loads if load == min_load]

        if len(candidates) == 1:
            selected = candidates[0]
            logger.info(
                f'Selected judge machine: {selected["name"]} '
                f'(load: {min_load}, only idle candidate)'
            )
            return selected

        total_weight = sum(m.get('weight', 1) for m in candidates)
        rand = random.uniform(0, total_weight)
        current = 0
        for m in candidates:
            current += m.get('weight', 1)
            if rand <= current:
                logger.info(
                    f'Selected judge machine: {m["name"]} '
                    f'(load: {min_load}, weighted among {len(candidates)} candidates)'
                )
                return m

        return candidates[-1]

    def get_queue_for_machine(self, machine):
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


load_balancer = JudgeLoadBalancer()
