from django.http import JsonResponse
from django.db import connection
from django.core.cache import cache
from django.conf import settings
from redis import Redis

from submissions.judge_health import (
    check_docker_daemon,
    check_judge_images,
    evaluate_machine_health,
)
from submissions.judge_load_balancer import load_balancer


def health_check(request):
    """Health check endpoint for monitoring."""
    health_status = {
        'status': 'healthy',
        'checks': {},
    }

    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        health_status['checks']['database'] = 'ok'
    except Exception as exc:
        health_status['status'] = 'unhealthy'
        health_status['checks']['database'] = f'error: {exc}'

    try:
        password = settings.CACHES['default'].get('OPTIONS', {}).get('PASSWORD') or None
        loc = settings.CACHES['default']['LOCATION']
        # redis://[:password@]host:port/db
        loc_body = loc.replace('redis://', '')
        if '@' in loc_body:
            loc_body = loc_body.split('@', 1)[1]
        host_port, _, db = loc_body.partition('/')
        host, _, port = host_port.partition(':')
        redis_client = Redis(host=host, port=int(port or 6379), db=int(db or 1), password=password)
        redis_client.ping()
        health_status['checks']['cache_redis'] = 'ok'
    except Exception as exc:
        health_status['status'] = 'unhealthy'
        health_status['checks']['cache_redis'] = f'error: {exc}'

    try:
        cache.set('health_check', 'ok', 10)
        cache.get('health_check')
        health_status['checks']['cache'] = 'ok'
    except Exception as exc:
        health_status['status'] = 'unhealthy'
        health_status['checks']['cache'] = f'error: {exc}'

    if getattr(settings, 'OJ_ROLE', 'web') == 'worker':
        docker_ok, docker_detail = check_docker_daemon()
        health_status['checks']['docker'] = 'ok' if docker_ok else docker_detail
        if not docker_ok:
            health_status['status'] = 'unhealthy'
        else:
            images_ok, images_detail = check_judge_images()
            health_status['checks']['judge_images'] = 'ok' if images_ok else images_detail
            if not images_ok:
                health_status['status'] = 'unhealthy'

    if getattr(settings, 'OJ_MULTI_JUDGE_ENABLED', False):
        judge_checks = {}
        for machine in load_balancer.get_enabled_machines():
            redis_client = load_balancer._machine_redis(machine, decode_responses=True)
            checks = evaluate_machine_health(machine, redis_client, check_local_docker=False)
            machine_ok = checks['redis'][0] and checks['worker'][0]
            judge_checks[machine['name']] = {
                'healthy': machine_ok,
                'redis': checks['redis'][1],
                'worker': checks['worker'][1],
            }
            if not machine_ok:
                health_status['status'] = 'unhealthy'
        health_status['checks']['judge_machines'] = judge_checks

    status_code = 200 if health_status['status'] == 'healthy' else 503
    return JsonResponse(health_status, status=status_code)
