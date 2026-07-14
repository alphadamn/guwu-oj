from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse

from submissions.judge_health import evaluate_machine_health
from submissions.judge_load_balancer import load_balancer


def health_check(request):
    """
    Health check endpoint for monitoring.
    Returns 200 if all services are healthy, 503 otherwise.
    """
    health_status = {
        'status': 'healthy',
        'checks': {}
    }
    
    # Check database connection
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        health_status['checks']['database'] = 'ok'
    except Exception as e:
        health_status['status'] = 'unhealthy'
        health_status['checks']['database'] = f'error: {str(e)}'
    
    # Check the configured cache backend without duplicating its connection
    # settings or parsing credentials from a URL.
    try:
        cache.set('health_check_redis', 'ok', 10)
        if cache.get('health_check_redis') != 'ok':
            raise RuntimeError('cache write was not persisted')
        health_status['checks']['redis'] = 'ok'
    except Exception as e:
        health_status['status'] = 'unhealthy'
        health_status['checks']['redis'] = f'error: {str(e)}'
    
    # Check cache
    try:
        cache.set('health_check', 'ok', 10)
        cache.get('health_check')
        health_status['checks']['cache'] = 'ok'
    except Exception as e:
        health_status['status'] = 'unhealthy'
        health_status['checks']['cache'] = f'error: {str(e)}'
    
    if getattr(settings, 'OJ_MULTI_JUDGE_ENABLED', False):
        judge_machines = {}
        for machine in load_balancer.get_enabled_machines():
            redis_client = load_balancer._machine_redis(machine, decode_responses=True)
            checks = evaluate_machine_health(machine, redis_client)
            machine_ok = checks['redis'][0] and checks['worker'][0]
            judge_machines[machine['name']] = {
                'healthy': machine_ok,
                'redis': checks['redis'][1],
                'worker': checks['worker'][1],
            }
            if not machine_ok:
                health_status['status'] = 'unhealthy'
        health_status['checks']['judge_machines'] = judge_machines

    status_code = 200 if health_status['status'] == 'healthy' else 503
    return JsonResponse(health_status, status=status_code)
