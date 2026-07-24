import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote, urlencode

from dotenv import load_dotenv

from .logging_config import LOGGING

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

DEMO_MODE = os.environ.get('DEMO_MODE', 'false').lower() in ('1', 'true', 'yes')
TEST_MODE = 'test' in sys.argv

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', '')
if not SECRET_KEY and not DEMO_MODE and not TEST_MODE:
    raise ValueError('DJANGO_SECRET_KEY must be set in environment or .env')

DEBUG = os.environ.get('DJANGO_DEBUG', 'false').lower() in ('1', 'true', 'yes')

ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get('DJANGO_ALLOWED_HOSTS', '*').split(',')
    if h.strip()
]

_csrf_origins = os.environ.get('DJANGO_CSRF_TRUSTED_ORIGINS', '')
CSRF_TRUSTED_ORIGINS = sorted({
    origin.strip()
    for origin in (
        _csrf_origins.split(',')
        + [
            'http://guwu.camluni.cn',
            'http://guwu.camluni.cn:3001',
            'https://guwu.camluni.cn',
            'https://guwu.camluni.cn:3001',
        ]
    )
    if origin.strip()
})


INSTALLED_APPS = [
    # SimpleUI must be registered before django.contrib.admin
    'simpleui',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'crispy_forms',
    'django_crontab',
    'crispy_bootstrap5',
    'users',
    'points',
    'problems',
    'submissions',
    'contests',
    'handbook',
    'mathfilters',
    'search',
    'django_rq',
    'django_ratelimit',
    'django_prometheus',
    'health',
    'devlog',
]

if not TEST_MODE:
    INSTALLED_APPS.append('sslserver')

MIDDLEWARE = [
    'django_prometheus.middleware.PrometheusBeforeMiddleware',
    'django.middleware.security.SecurityMiddleware',
    # StaticCacheHeaders MUST wrap WhiteNoise so it can reapply
    # Cache-Control on WhiteNoise's short-circuited static responses.
    'devlog.middleware.StaticCacheHeaders',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'users.middleware.EnforcementMiddleware',
    'points.middleware.DailyCheckInMiddleware',
    'devlog.middleware.TrafficMetricsMiddleware',
    'django_prometheus.middleware.PrometheusAfterMiddleware',
]

ROOT_URLCONF = 'oj_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'devlog.context_processors.oj_site',
            ],
        },
    },
]

WSGI_APPLICATION = 'oj_project.wsgi.application'

def _env_enabled(name, default=True):
    return os.environ.get(name, str(default)).lower() in ('1', 'true', 'yes')


def _parse_judge_machines(raw, fallback):
    """Parse and validate the optional JSON-based judge machine config."""
    raw = raw or ''
    if not raw.strip():
        return fallback

    try:
        machines = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError('JUDGE_MACHINES_JSON must contain valid JSON') from exc

    if not isinstance(machines, list) or not machines:
        raise ValueError('JUDGE_MACHINES_JSON must be a non-empty JSON array')

    validated = []
    queues = set()
    for index, machine in enumerate(machines, start=1):
        if not isinstance(machine, dict):
            raise ValueError(f'JUDGE_MACHINES_JSON item {index} must be an object')

        name = machine.get('name')
        host = machine.get('host')
        queue = machine.get('queue')
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f'JUDGE_MACHINES_JSON item {index} requires a non-empty name')
        if not isinstance(host, str) or not host.strip():
            raise ValueError(f'JUDGE_MACHINES_JSON item {index} requires a non-empty host')
        if not isinstance(queue, str) or not queue.strip():
            raise ValueError(f'JUDGE_MACHINES_JSON item {index} requires a non-empty queue')
        name = name.strip()
        host = host.strip()
        queue = queue.strip()
        if queue in queues:
            raise ValueError(f'JUDGE_MACHINES_JSON has duplicate queue: {queue}')
        queues.add(queue)

        try:
            port = int(machine.get('port', 6379))
            db = int(machine.get('db', 0))
            weight = int(machine.get('weight', 1))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f'JUDGE_MACHINES_JSON item {index} has invalid port, db, or weight'
            ) from exc
        if not 1 <= port <= 65535 or db < 0 or weight < 1:
            raise ValueError(
                f'JUDGE_MACHINES_JSON item {index} has out-of-range port, db, or weight'
            )

        enabled = machine.get('enabled', True)
        if not isinstance(enabled, bool):
            raise ValueError(f'JUDGE_MACHINES_JSON item {index} enabled must be boolean')

        tls = machine.get('tls', False)
        if not isinstance(tls, bool):
            raise ValueError(f'JUDGE_MACHINES_JSON item {index} tls must be boolean')
        password = machine.get('password', '')
        ca_cert_path = machine.get('ca_cert_path', '')
        client_cert_path = machine.get('client_cert_path', '')
        client_key_path = machine.get('client_key_path', '')
        for field_name, value in {
            'password': password,
            'ca_cert_path': ca_cert_path,
            'client_cert_path': client_cert_path,
            'client_key_path': client_key_path,
        }.items():
            if not isinstance(value, str):
                raise ValueError(
                    f'JUDGE_MACHINES_JSON item {index} {field_name} must be a string'
                )
        if tls and not ca_cert_path.strip():
            raise ValueError(f'JUDGE_MACHINES_JSON item {index} TLS requires ca_cert_path')
        if bool(client_cert_path.strip()) != bool(client_key_path.strip()):
            raise ValueError(
                f'JUDGE_MACHINES_JSON item {index} requires both client_cert_path and client_key_path'
            )

        validated.append({
            'name': name,
            'host': host,
            'port': port,
            'db': db,
            'queue': queue,
            'enabled': enabled,
            'weight': weight,
            'tls': tls,
            'password': password,
            'ca_cert_path': ca_cert_path.strip(),
            'client_cert_path': client_cert_path.strip(),
            'client_key_path': client_key_path.strip(),
        })
    #print(validated)
    return validated


def _redis_tls_kwargs(enabled, ca_cert_path, client_cert_path='', client_key_path='', direct=False):
    #print(ca_cert_path)
    if not enabled:
        return {}
    kwargs = {
        'ssl_cert_reqs': 'required',
        'ssl_ca_certs': ca_cert_path,
    }
    if client_cert_path:
        kwargs['ssl_certfile'] = client_cert_path
        kwargs['ssl_keyfile'] = client_key_path
    if direct:
        kwargs['ssl'] = True
    return kwargs


def _rq_machine_connection(machine):
    """Return Redis client settings for one queue without exposing credentials in URLs."""
    tls_enabled = machine.get('tls', _env_enabled('RQ_REDIS_TLS'))
    ca_cert_path = machine.get(
        'ca_cert_path', os.environ.get('RQ_REDIS_CA_CERT', '/etc/redis/tls/ca.crt')
    )
    client_cert_path = machine.get(
        'client_cert_path', os.environ.get('RQ_REDIS_CLIENT_CERT', '')
    )
    client_key_path = machine.get(
        'client_key_path', os.environ.get('RQ_REDIS_CLIENT_KEY', '')
    )
    password = machine.get('password') or _rq_redis_password()
    kwargs = {
        'socket_connect_timeout': 5,
        # RQ's blocking pub/sub listener must not inherit a short read timeout.
        # Health and load-balancer clients explicitly set their own 3-second
        # timeout in JudgeLoadBalancer._machine_redis.
        'socket_timeout': None,
        'retry_on_timeout': True,
    }
    if password:
        kwargs['password'] = password
    kwargs.update(_redis_tls_kwargs(
        tls_enabled, ca_cert_path, client_cert_path, client_key_path, direct=True,
    ))
    return kwargs


def _rq_redis_password():
    password = os.environ.get('RQ_REDIS_PASSWORD', '')
    if DEMO_MODE or TEST_MODE:
        return password
    if len(password) < 12:
        raise ValueError('RQ_REDIS_PASSWORD must be at least 12 characters long')
    if not any(char.isalpha() for char in password):
        raise ValueError('RQ_REDIS_PASSWORD must contain a letter')
    if not any(char.isdigit() for char in password):
        raise ValueError('RQ_REDIS_PASSWORD must contain a digit')
    if not any(not char.isalnum() for char in password):
        raise ValueError('RQ_REDIS_PASSWORD must contain a special character')
    return password


def _redis_url(host, port, db, password='', tls=False, ca_cert_path=''):
    scheme = 'rediss' if tls else 'redis'
    if password:
        url = f'{scheme}://:{quote(password, safe="")}@{host}:{port}/{db}'
    else:
        url = f'{scheme}://{host}:{port}/{db}'
    if tls:
        url = f'{url}?{urlencode(_redis_tls_kwargs(True, ca_cert_path))}'
    return url


def _rq_redis_kwargs():
    return _rq_machine_connection({})


def _rq_queue_entry(machine):
    connection = _rq_machine_connection(machine)
    client_kwargs = {
        key: value for key, value in connection.items()
        if key not in {'password', 'ssl', 'ssl_cert_reqs'}
    }
    return {
        'HOST': machine['host'],
        'PORT': machine['port'],
        'DB': machine['db'],
        'PASSWORD': connection.get('password'),
        'SSL': connection.get('ssl', False),
        'SSL_CERT_REQS': connection.get('ssl_cert_reqs', 'required'),
        'REDIS_CLIENT_KWARGS': client_kwargs,
        'DEFAULT_TIMEOUT': 3600,
        'WORKER_CLASS': 'oj_project.customrq.AutoReconnectWorker',
    }


if not DEMO_MODE:
    redis_host = os.environ.get('CACHE_REDIS_HOST', '127.0.0.1')
    redis_port = int(os.environ.get('CACHE_REDIS_PORT', '6379'))
    redis_db = int(os.environ.get('CACHE_REDIS_DB', '1'))
    redis_password = os.environ.get('CACHE_REDIS_PASSWORD', '')
    cache_redis_tls = _env_enabled('CACHE_REDIS_TLS')
    cache_redis_ca_cert = os.environ.get('CACHE_REDIS_CA_CERT', '/etc/redis/tls/ca.crt')
    CACHE_REDIS_CONNECTION_KWARGS = _redis_tls_kwargs(
        cache_redis_tls, cache_redis_ca_cert,
    )
    CACHE_REDIS_DIRECT_CONNECTION_KWARGS = _redis_tls_kwargs(
        cache_redis_tls, cache_redis_ca_cert, direct=True,
    )
    RQ_REDIS_CONNECTION_KWARGS = _rq_redis_kwargs()

    cache_options = {
        'CLIENT_CLASS': 'django_redis.client.DefaultClient',
        'SOCKET_KEEPALIVE': True,
        'CONNECTION_POOL_KWARGS': CACHE_REDIS_CONNECTION_KWARGS,
    }
    if redis_password:
        cache_options['PASSWORD'] = redis_password

    CACHES = {
        'default': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': _redis_url(
                redis_host, redis_port, redis_db, redis_password,
                cache_redis_tls, cache_redis_ca_cert,
            ),
            'OPTIONS': cache_options,
        }
    }

    # The web process connects to the judge Redis endpoint; the judge worker
    # connects to the same endpoint through loopback. Keep these deployment
    # addresses outside source control so both hosts can run one revision.
    rq_host = os.environ.get('RQ_REDIS_HOST', '127.0.0.1')
    rq_port = int(os.environ.get('RQ_REDIS_PORT', '6379'))
    rq_db = int(os.environ.get('RQ_REDIS_DB', '0'))
    judge_1_host = os.environ.get('JUDGE_1_HOST', rq_host)
    judge_1_port = int(os.environ.get('JUDGE_1_PORT', str(rq_port)))
    judge_1_db = int(os.environ.get('JUDGE_1_REDIS_DB', str(rq_db)))

    default_rq_machine = {
        'host': rq_host,
        'port': rq_port,
        'db': rq_db,
    }
    RQ_QUEUES = {
        'default': _rq_queue_entry(default_rq_machine),
        'high': _rq_queue_entry(default_rq_machine),
        'low': _rq_queue_entry(default_rq_machine),
    }

    default_judge_machines = [
        {
            'name': 'judge-1',
            'host': judge_1_host,
            'port': judge_1_port,
            'db': judge_1_db,
            'queue': 'judge-1',
            'enabled': True,
            'weight': 1,
            'tls': _env_enabled('RQ_REDIS_TLS'),
            'password': _rq_redis_password(),
            'ca_cert_path': os.environ.get('RQ_REDIS_CA_CERT', '/www/wwwroot/tls-judge/ca.crt'),
            'client_cert_path': os.environ.get('RQ_REDIS_CLIENT_CERT', '/www/wwwroot/tls-judge/redis.crt'),
            'client_key_path': os.environ.get('RQ_REDIS_CLIENT_KEY', '/www/wwwroot/tls-judge/redis.key'),
        },
    ]
    JUDGE_MACHINES = _parse_judge_machines(
        os.environ.get('JUDGE_MACHINES_JSON', ''),
        default_judge_machines,
    )
    # Local worker settings own the transport for the queue it consumes. The
    # web process alone reads JudgeMachine database overrides for remote queues.

    OJ_MULTI_JUDGE_ENABLED = os.environ.get('OJ_MULTI_JUDGE_ENABLED', 'true').lower() in ('1', 'true', 'yes')
    OJ_ROLE = os.environ.get('OJ_ROLE', 'web')
    OJ_JUDGE_QUEUE = os.environ.get('OJ_JUDGE_QUEUE', '')

    # A judge host normally has credentials only for its own local Redis endpoint.
    # Register that queue even when its web-side JUDGE_MACHINES_JSON lives solely
    # on the web host.
    if OJ_ROLE == 'worker' and OJ_JUDGE_QUEUE:
        RQ_QUEUES[OJ_JUDGE_QUEUE] = _rq_queue_entry(default_rq_machine)

    if not (OJ_ROLE == 'worker' and OJ_JUDGE_QUEUE):
        for machine in JUDGE_MACHINES:
            if machine.get('enabled', True):
                RQ_QUEUES[machine['queue']] = _rq_queue_entry(machine)

    RQ = {
        'exception_handler': 'django_rq.handlers.sentry',
    }
else:
    CACHE_REDIS_CONNECTION_KWARGS = {}
    CACHE_REDIS_DIRECT_CONNECTION_KWARGS = {}
    RQ_REDIS_CONNECTION_KWARGS = {}
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.filebased.FileBasedCache',
            'LOCATION': os.path.join(tempfile.gettempdir(), 'oj_demo_cache'),
        }
    }
    RQ_QUEUES = {}
    JUDGE_MACHINES = []
    OJ_MULTI_JUDGE_ENABLED = False
    OJ_JUDGE_QUEUE = ''

if DEMO_MODE:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.environ.get('DB_NAME', 'ojdb'),
            'USER': os.environ.get('DB_USER', 'ojuser'),
            'PASSWORD': os.environ.get('DB_PASSWORD', ''),
            'HOST': os.environ.get('DB_HOST', '127.0.0.1'),
            'PORT': os.environ.get('DB_PORT', '5432'),
            'OPTIONS': {
                'sslmode': os.environ.get('DB_SSLMODE', 'require'),
                **(
                    {'sslrootcert': os.environ['DB_SSLROOTCERT']}
                    if os.environ.get('DB_SSLROOTCERT')
                    else {}
                ),
            },
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

LANGUAGE_CODE = 'zh-hans'

TIME_ZONE = 'Asia/Shanghai'

USE_I18N = True

USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']

WHITENOISE_ROOT = BASE_DIR / 'static'
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = True
# Base max-age for WhiteNoise.  Per-request override (and admin-configurable
# TTL) is applied by devlog.middleware.StaticCacheHeaders.
WHITENOISE_MAX_AGE = 86400

# ---------------------------------------------------------------------------
# Reverse proxy + security headers
# ---------------------------------------------------------------------------
# When running behind nginx, the real client IP is carried in
# ``X-Forwarded-For`` and the original scheme is in ``X-Forwarded-Proto``.
# Django needs to trust these headers for things like ``request.is_secure()``
# and password-reset emails to produce ``https://`` links.

# Trust ``X-Forwarded-For`` / ``X-Forwarded-Host`` / ``X-Forwarded-Port``.
# When enabled, ``request.META['REMOTE_ADDR']`` is taken from the last proxy
# in ``X-Forwarded-For``; the application code uses its own ``_client_ip()``
# helper to grab the *first* (real client) entry.
USE_X_FORWARDED_HOST = os.environ.get('USE_X_FORWARDED_HOST', 'true').lower() in ('1', 'true', 'yes')
USE_X_FORWARDED_PORT = os.environ.get('USE_X_FORWARDED_PORT', 'true').lower() in ('1', 'true', 'yes')

# ``SECURE_PROXY_SSL_HEADER`` tells Django: "when the upstream proxy sets the
# header HTTP_X_FORWARDED_PROTO to 'https', treat the request as secure".
# Format in the env var: ``<HTTP_HEADER_NAME>,<expected_value>``.
_spsh = os.environ.get('SECURE_PROXY_SSL_HEADER', '')
if _spsh:
    try:
        _spsh_header, _spsh_value = [x.strip() for x in _spsh.split(',', 1)]
        SECURE_PROXY_SSL_HEADER = (_spsh_header, _spsh_value)
    except ValueError:
        pass

# Only apply the "secure" cookies & HSTS when outside of dev/test.
#if not (TEST_MODE or DEBUG):
#    SESSION_COOKIE_SECURE = os.environ.get('SESSION_COOKIE_SECURE', 'true').lower() in ('1', 'true', 'yes')
#    CSRF_COOKIE_SECURE = os.environ.get('CSRF_COOKIE_SECURE', 'true').lower() in ('1', 'true', 'yes')
#    SECURE_SSL_REDIRECT = os.environ.get('SECURE_SSL_REDIRECT', 'true').lower() in ('1', 'true', 'yes')
#    try:
#        SECURE_HSTS_SECONDS = int(os.environ.get('SECURE_HSTS_SECONDS', '31536000'))
#    except ValueError:
#        SECURE_HSTS_SECONDS = 31536000
#    SECURE_HSTS_INCLUDE_SUBDOMAINS = os.environ.get('SECURE_HSTS_INCLUDE_SUBDOMAINS', 'true').lower() in ('1', 'true', 'yes')
#    SECURE_HSTS_PRELOAD = os.environ.get('SECURE_HSTS_PRELOAD', 'true').lower() in ('1', 'true', 'yes')
#    SECURE_BROWSER_XSS_FILTER = os.environ.get('SECURE_BROWSER_XSS_FILTER', 'true').lower() in ('1', 'true', 'yes')
#    SECURE_CONTENT_TYPE_NOSNIFF = os.environ.get('SECURE_CONTENT_TYPE_NOSNIFF', 'true').lower() in ('1', 'true', 'yes')

# A tiny helper used by ``users/captcha.py::_client_ip`` and by
# ``users/middleware.py::EnforcementMiddleware`` to detect internal proxy
# "noise" IPs (e.g. SimpleUI iframe requests) when computing rate limits.
TRUSTED_PROXY_IPS = [h.strip() for h in os.environ.get('TRUSTED_PROXY_IPS', '127.0.0.1,::1').split(',') if h.strip()]

# Optional local MaxMind GeoLite2 country database for anonymous dashboard aggregation.
GEOIP2_COUNTRY_DB = os.environ.get('GEOIP2_COUNTRY_DB', str(BASE_DIR / 'data' / 'GeoLite2-Country.mmdb'))

# Optional server endpoint used as the destination of dashboard request arcs.
OJ_SERVER_IP = os.environ.get('OJ_SERVER_IP', '')

AUTH_USER_MODEL = 'users.User'

CRISPY_ALLOWED_TEMPLATE_PACKS = "bootstrap5"

CRISPY_TEMPLATE_PACK = "bootstrap5"

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'home'
LOGOUT_REDIRECT_URL = 'home'

OJ_SITE_NAME = '谷物 OJ'

# Submission limits
OJ_MAX_SUBMISSION_CODE_BYTES = int(
    os.environ.get('OJ_MAX_SUBMISSION_CODE_BYTES', str(256 * 1024))
)

# Sandbox judge (Docker, no network inside container)
OJ_DOCKER_ENABLED = os.environ.get('OJ_DOCKER_ENABLED', 'true').lower() in ('1', 'true', 'yes')
OJ_DOCKER_IMAGE = os.environ.get('OJ_DOCKER_IMAGE', 'oj-judge:latest')
OJ_DOCKER_PIDS_LIMIT = int(os.environ.get('OJ_DOCKER_PIDS_LIMIT', '64'))
OJ_DOCKER_NOFILE_LIMIT = int(os.environ.get('OJ_DOCKER_NOFILE_LIMIT', '64'))
# The profile must be loaded on every judge host before containers are started.
OJ_DOCKER_APPARMOR_PROFILE = os.environ.get('OJ_DOCKER_APPARMOR_PROFILE', 'oj-judge').strip()
# Default subprocess timeout (can be overridden via JudgeConfig model in admin)
OJ_SUBPROCESS_TIMEOUT_SEC = int(os.environ.get('OJ_SUBPROCESS_TIMEOUT_SEC', '5'))

# SigmaIDE embed — nginx path /sigmaide/ (never :3004). Override in .env if needed.
SIGMAIDE_BASE_URL = os.environ.get('SIGMAIDE_BASE_URL', '').rstrip('/')

# Logging configuration
LOGGING = LOGGING

# ---------------------------------------------------------------------------
# Email configuration (SMTP / send test emails / health alerts)
# ---------------------------------------------------------------------------
# All sensitive values (host user, password, from address) come from
# ``.env`` / the process environment and are *never* stored in the database.
# The ``devlog.models.EmailConfig`` row in PostgreSQL keeps only the
# *metadata*: host, port, TLS/SSL flags, timeout and the admin recipient
# list; its ``email_host_password`` field is kept as an optional override
# and is rendered with a password-style widget in the admin.

EMAIL_BACKEND = os.environ.get(
    'EMAIL_BACKEND',
    'django.core.mail.backends.smtp.EmailBackend',
)
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp-relay.brevo.com')
try:
    EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
except ValueError:
    EMAIL_PORT = 587
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'true').lower() in ('1', 'true', 'yes')
EMAIL_USE_SSL = os.environ.get('EMAIL_USE_SSL', 'false').lower() in ('1', 'true', 'yes')
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', '')
SERVER_EMAIL = os.environ.get('SERVER_EMAIL', '') or DEFAULT_FROM_EMAIL

# ``MANAGERS`` / ``ADMINS`` — used by ``mail_managers`` / ``mail_admins``.
def _parse_admin_csv(raw):
    """Parse ``"Name <email@x>, Another <b@y>"`` into a list of ``(name, email)`` tuples."""
    if not raw:
        return []
    import re as _re
    result = []
    for part in raw.split(','):
        part = part.strip().strip('"').strip("'")
        if not part:
            continue
        m = _re.match(r'^\s*(.+?)\s*<([^>]+)>\s*$', part)
        if m:
            result.append((m.group(1).strip(), m.group(2).strip()))
        elif '@' in part:
            result.append(('', part))
    return result

ADMINS = tuple(_parse_admin_csv(os.environ.get('ADMINS_CSV', ''))) or (
    ('admin', SERVER_EMAIL),
)
MANAGERS = ADMINS


CRONJOBS = [
    # 第一个参数是 cron 时间表达式，第二个参数是任务函数的 Python 路径
    ('*/30 * * * *', 'devlog.views._refresh_auto_components', [], {'force_refresh': True}),
    ('*/5 * * * *', 'contests.jobs.publish_finished_contests_job'),
]


# ---------------------------------------------------------------------------
# django-simpleui
# ---------------------------------------------------------------------------
# SimpleUI 是 Django Admin 的现代化 Vue 主题，基于 ElementUI。
# 参考：https://simpleui.72wo.com/docs/simpleui

# SimpleUI 管理中心首页（默认展示 Django admin 的仪表盘）。
# 注意：SIMPLEUI_HOME_PAGE 设置为 '/' 会在 admin 首页加载整站首页，
# 应留空（不包含此键）或设置为 '/admin/' 让 SimpleUI 显示默认首页。
SIMPLEUI_HOME_TITLE = '谷物 OJ 管理中心'
# SIMPLEUI_HOME_PAGE 不要显式设置，让 SimpleUI 显示默认 admin 首页。
SIMPLEUI_HOME_INFO = False   # 关闭右上角 SimpleUI 官方资讯
SIMPLEUI_ANALYSIS = False    # 关闭统计
SIMPLEUI_LOGO = '/apple-touch-icon.png'
# SimpleUI 菜单中 "url" 字段会被视为相对 SIMPLEUI_INDEX 的相对路径。
# 由于我们在菜单中使用不带 "/admin/" 前缀的 app/model 路径，
# 此处将 SIMPLEUI_INDEX 设为 '/admin/' 以拼接成完整路径。
SIMPLEUI_INDEX = '/admin/'

# 主题：'Default / dark | 2023 年开始 simpleui 支持多主题。
# 使用 SimpleUI 安装包中实际存在的主题文件名。
SIMPLEUI_DEFAULT_THEME = 'light.css'

# 站点信息 / 登录页面标题
# 手动构建菜单。SimpleUI 对每个菜单模型项会生成递增内部 eid (从 1001 开始)。
# 为避免 eid 错位，我们显式提供完整菜单，且 models 列表项与真实 Django URL 一致。
SIMPLEUI_CONFIG = {
    'system_keep': False,
    'dynamic': False,
    'menus': [
        {
            'name': '用户管理',
            'icon': 'fas fa-user-friends',
            'models': [
                {'name': '用户', 'icon': 'fas fa-user',
                 'url': '/admin/users/user/'},
                {'name': '用户组', 'icon': 'fas fa-users',
                 'url': '/admin/auth/group/'},
                {'name': '处罚记录', 'icon': 'fas fa-gavel',
                 'url': '/admin/users/userpunishment/'},
                {'name': 'IP 封禁', 'icon': 'fas fa-ban',
                 'url': '/admin/users/ipban/'},
            ],
        },
        {
            'name': '题目管理',
            'icon': 'fas fa-book',
            'models': [
                {'name': '题目', 'icon': 'fas fa-file-alt',
                 'url': '/admin/problems/problem/'},
                {'name': '测试用例', 'icon': 'fas fa-file-code',
                 'url': '/admin/problems/testcase/'},
                {'name': '官方题解', 'icon': 'fas fa-lightbulb',
                 'url': '/admin/problems/solution/'},
            ],
        },
        {
            'name': '评测与提交',
            'icon': 'fas fa-paper-plane',
            'models': [
                {'name': '提交记录', 'icon': 'fas fa-list',
                 'url': '/admin/submissions/submission/'},
                {'name': '评测机', 'icon': 'fas fa-microchip',
                 'url': '/admin/submissions/judgemachine/'},
                {'name': '评测配置', 'icon': 'fas fa-sliders-h',
                 'url': '/admin/submissions/judgeconfig/'},
            ],
        },
        {
            'name': '积分管理',
            'icon': 'fas fa-coins',
            'models': [
                {'name': '积分配置', 'icon': 'fas fa-sliders-h',
                 'url': '/admin/points/pointconfig/'},
                {'name': '积分流水', 'icon': 'fas fa-receipt',
                 'url': '/admin/points/pointledgerentry/'},
                {'name': '每日签到', 'icon': 'fas fa-calendar-check',
                 'url': '/admin/points/dailycheckin/'},
            ],
        },
        {
            'name': '竞赛管理',
            'icon': 'fas fa-trophy',
            'models': [
                {'name': '竞赛', 'icon': 'fas fa-flag-checkered',
                 'url': '/admin/contests/contest/'},
                {'name': '竞赛题目', 'icon': 'fas fa-list-ol',
                 'url': '/admin/contests/contestproblem/'},
            ],
        },
        {
            'name': '开发日志',
            'icon': 'fas fa-th-list',
            'models': [
                {'name': '服务组件', 'icon': 'fas fa-server',
                 'url': '/admin/devlog/servicecomponent/'},
                {'name': '健康样本', 'icon': 'fas fa-chart-line',
                 'url': '/admin/devlog/healthsample/'},
                {'name': '开发者日志', 'icon': 'fas fa-book-open',
                 'url': '/admin/devlog/devlogentry/'},
                {'name': '文件变更记录', 'icon': 'fas fa-file-contract',
                 'url': '/admin/devlog/filechange/'},
                {'name': '文件快照', 'icon': 'fas fa-archive',
                 'url': '/admin/devlog/filesnapshot/'},
            ],
        },
        {
            'name': '系统配置',
            'icon': 'fas fa-cog',
            'models': [
                {'name': '.env 配置生成器', 'icon': 'fas fa-file-code',
                 'url': '/admin/env-generator/'},
                {'name': '缓存配置', 'icon': 'fas fa-database',
                 'url': '/admin/devlog/cacheconfig/'},
                {'name': '验证码配置', 'icon': 'fas fa-shield-alt',
                 'url': '/admin/devlog/captchaconfig/'},
                {'name': '邮件配置', 'icon': 'fas fa-envelope',
                 'url': '/admin/devlog/emailconfig/'},
                {'name': '注册配置', 'icon': 'fas fa-user-plus',
                 'url': '/admin/devlog/registrationconfig/'},
                {'name': '站点配置', 'icon': 'fas fa-palette',
                 'url': '/admin/devlog/siteconfig/'},
                {'name': '健康检查配置', 'icon': 'fas fa-heartbeat',
                 'url': '/admin/devlog/healthcheckconfig/'},
            ],
        },
    ],
}

# 首页：本地仪表盘替换 SimpleUI 的快捷入口，保留最近操作时间线。
SIMPLEUI_HOME_QUICK = False
SIMPLEUI_HOME_ACTION = True

# 首页默认模块（首页右侧信息 / 历史 / 快捷操作等
# - true 显示，false 关闭
SIMPLEUI_STATIC_OFFLINE = False
