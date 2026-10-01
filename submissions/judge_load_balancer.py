"""Judge machine registry and health probes.

With the central Celery broker this module no longer dispatches work: every
worker competes on the single ``judge`` queue. It only keeps the list of
judge Redis endpoints (settings + ``JudgeMachine`` admin overrides) used for:

* fleet health checks (``check_judge_health``, admin actions, ``/health/``);
* the WebSocket consumer's per-machine Redis pub/sub subscriptions.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


class JudgeLoadBalancer:
    def __init__(self):
        self.multi_judge_enabled = getattr(settings, 'OJ_MULTI_JUDGE_ENABLED', False)
        self.health_check_cache_prefix = 'judge_health_'
        self.health_check_ttl = 30

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
                    'enabled': db_machine.enabled,
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
        from oj_project.settings import _judge_redis_connection_kwargs

        kwargs = _judge_redis_connection_kwargs(machine)
        kwargs.update({
            'host': machine['host'],
            'port': machine['port'],
            'db': machine['db'],
            'socket_connect_timeout': 3,
            'socket_timeout': 3,
            'decode_responses': decode_responses,
        })
        return redis.Redis(**kwargs)

    def _find_machine(self, name):
        for m in self.machines:
            if m.get('name') == name:
                return m
        return None

    def get_enabled_machines(self):
        """Get list of enabled judge machines."""
        if not self.multi_judge_enabled:
            return []
        return [m for m in self.machines if m.get('enabled', True)]

    def check_machine_health(self, machine):
        """Check Redis, fleet worker heartbeat, and optionally local Docker."""
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
            logger.debug('Judge machine %s is healthy', machine['name'])
        else:
            failed = {name: detail for name, (ok, detail) in checks.items() if not ok}
            logger.warning('Judge machine %s health check failed: %s', machine['name'], failed)

        cache.set(cache_key, is_healthy, self.health_check_ttl)
        return is_healthy


load_balancer = JudgeLoadBalancer()
