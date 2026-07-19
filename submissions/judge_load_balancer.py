"""Load balancer for distributing judge tasks across multiple judge machines."""

import logging
import os
import random
import time
from contextlib import contextmanager
from django.conf import settings

logger = logging.getLogger(__name__)


class JudgeLoadBalancer:
    """Load balancer for distributing judge tasks across multiple judge machines."""

    def __init__(self):
        self.multi_judge_enabled = getattr(settings, 'OJ_MULTI_JUDGE_ENABLED', False)
        self.health_check_cache_prefix = 'judge_health_'
        self.health_check_ttl = 30
        self.selection_lock_key = 'judge:selection:lock'
        self.selection_lock_ttl = 10
        self.selection_lock_wait_sec = 3

    @property
    def machines(self):
        """Return worker-local settings or web-side admin overrides."""
        configured = {
            machine['name']: dict(machine)
            for machine in getattr(settings, 'JUDGE_MACHINES', [])
        }
        if getattr(settings, 'OJ_ROLE', 'web') == 'worker':
            return list(configured.values())

        from submissions.models import JudgeMachine
        try:
            for db_machine in JudgeMachine.objects.all():
                machine = configured.get(db_machine.name, {})
                machine.update({
                    'name': db_machine.name,
                    'host': db_machine.host,
                    'port': db_machine.port,
                    'db': db_machine.db,
                    'queue': db_machine.queue,
                    'enabled': db_machine.enabled,
                    'weight': db_machine.weight,
                })
                if db_machine.transport_configured:
                    machine.update({
                        'tls': db_machine.tls_enabled,
                        'ca_cert_path': db_machine.ca_cert_path,
                        'client_cert_path': db_machine.client_cert_path,
                        'client_key_path': db_machine.client_key_path,
                        'password': db_machine.get_redis_password(),
                    })
                configured[db_machine.name] = machine
        except Exception:
            pass
        return list(configured.values())

    def effective_machine(self, name):
        return self._find_machine(name=name)

    def _machine_redis(self, machine, decode_responses=False):
        import redis
        from oj_project.settings import _rq_machine_connection

        kwargs = _rq_machine_connection(machine)
        kwargs.update({
            'host': machine['host'],
            'port': machine['port'],
            'db': machine['db'],
            'socket_connect_timeout': 3,
            'socket_timeout': 3,
            'decode_responses': decode_responses,
        })
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

    @contextmanager
    def _selection_lock(self):
        """Serialize select-and-reserve across web requests."""
        from django.core.cache import cache

        deadline = time.monotonic() + self.selection_lock_wait_sec
        acquired = False
        while time.monotonic() < deadline:
            acquired = cache.add(
                self.selection_lock_key, 'locked', self.selection_lock_ttl,
            )
            if acquired:
                break
            time.sleep(0.05)

        if not acquired:
            raise TimeoutError('Timed out waiting to reserve judge capacity')

        try:
            yield
        finally:
            cache.delete(self.selection_lock_key)

    def _busy_key(self, machine):
        return f"judge:busy:{machine['name']}"

    def _get_queue_length(self, machine):
        try:
            return self._machine_redis(machine).llen(f"rq:queue:{machine['queue']}")
        except Exception:
            return 9999

    def _get_busy_count(self, machine):
        try:
            return int(self._machine_redis(machine).get(self._busy_key(machine)) or 0)
        except Exception:
            return 9999

    def _incr_busy(self, machine):
        try:
            r = self._machine_redis(machine)
            key = self._busy_key(machine)
            val = r.incr(key)
            r.expire(key, 3600)
            logger.debug('Reserved busy slot for %s: %s', machine['name'], val)
            return val
        except Exception:
            logger.exception('Failed to increment busy count for %s', machine.get('name'))
            raise

    def _decr_busy(self, machine):
        try:
            r = self._machine_redis(machine)
            key = self._busy_key(machine)
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

    def reserve_machine(self, submission_id):
        """Select and reserve one judge capacity slot before queueing a job."""
        if not self.multi_judge_enabled:
            return None

        with self._selection_lock():
            healthy_machines = self.get_healthy_machines()
            if not healthy_machines:
                logger.warning('No healthy judge machines available, falling back to default queue')
                return None

            machine_loads = []
            for machine in healthy_machines:
                queue_length = self._get_queue_length(machine)
                busy = self._get_busy_count(machine)
                total_load = queue_length + busy
                machine_loads.append((total_load, machine, queue_length, busy))
                logger.debug(
                    'Judge %s load: queue=%s, busy=%s, total=%s',
                    machine['name'], queue_length, busy, total_load,
                )

            machine_loads.sort(key=lambda item: item[0])
            min_load = machine_loads[0][0]
            candidates = [item for item in machine_loads if item[0] == min_load]

            if len(candidates) == 1:
                _, selected, queue_length, busy = candidates[0]
                reason = 'only lowest-load candidate'
            else:
                total_weight = sum(machine.get('weight', 1) for _, machine, _, _ in candidates)
                threshold = random.uniform(0, total_weight)
                running_weight = 0
                selected, queue_length, busy = candidates[-1][1:]
                for _, machine, candidate_queue_length, candidate_busy in candidates:
                    running_weight += machine.get('weight', 1)
                    if threshold <= running_weight:
                        selected = machine
                        queue_length = candidate_queue_length
                        busy = candidate_busy
                        break
                reason = f'weighted among {len(candidates)} lowest-load candidates'

            reserved_busy = self._incr_busy(selected)
            try:
                self._set_submission_machine(submission_id, selected['name'])
            except Exception:
                self._decr_busy(selected)
                raise

            logger.info(
                'Reserved judge machine %s for submission %s '
                '(queue=%s, busy=%s, load=%s, reserved_busy=%s, %s)',
                selected['name'], submission_id, queue_length, busy, min_load,
                reserved_busy, reason,
            )
            return selected

    def select_machine(self):
        """Return the least-loaded machine without reserving capacity.

        Use reserve_machine() for actual submission dispatch.
        """
        if not self.multi_judge_enabled:
            return None

        healthy_machines = self.get_healthy_machines()
        if not healthy_machines:
            logger.warning('No healthy judge machines available, falling back to default queue')
            return None

        machine_loads = []
        for machine in healthy_machines:
            queue_length = self._get_queue_length(machine)
            busy = self._get_busy_count(machine)
            total_load = queue_length + busy
            machine_loads.append((total_load, machine))
            logger.debug('Judge %s load: queue=%s, busy=%s, total=%s', machine['name'], queue_length, busy, total_load)

        machine_loads.sort(key=lambda item: item[0])
        min_load = machine_loads[0][0]
        candidates = [machine for load, machine in machine_loads if load == min_load]
        if len(candidates) == 1:
            return candidates[0]

        total_weight = sum(machine.get('weight', 1) for machine in candidates)
        threshold = random.uniform(0, total_weight)
        running_weight = 0
        for machine in candidates:
            running_weight += machine.get('weight', 1)
            if threshold <= running_weight:
                return machine
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
