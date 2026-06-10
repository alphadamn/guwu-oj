import os
import sys
import tempfile
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Logging configuration
from .logging_config import LOGGING

SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'wfdscw34rewfdsg54y3redwqefrg45t3ewffr34we',
)

DEBUG = os.environ.get('DJANGO_DEBUG', 'false').lower() in ('1', 'true', 'yes')

# Demo mode: disable PostgreSQL and Redis for demo purposes
DEMO_MODE = os.environ.get('DEMO_MODE', 'false').lower() in ('1', 'true', 'yes')

TEST_MODE = 'test' in sys.argv

ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get('DJANGO_ALLOWED_HOSTS', '*').split(',')
    if h.strip()
]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    # 'django.contrib.staticfiles',
    'crispy_forms',
    'crispy_bootstrap5',
    'users',
    'problems',
    'submissions',
    'handbook',
    'mathfilters',
    'search',
    'django_rq',
    'django_ratelimit',
    'django_prometheus',
    'health',
]

if not TEST_MODE:
    INSTALLED_APPS.append('sslserver')

MIDDLEWARE = [
    'django_prometheus.middleware.PrometheusBeforeMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
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
            ],
        },
    },
]

WSGI_APPLICATION = 'oj_project.wsgi.application'

if not DEMO_MODE:
    redis_host = '127.0.0.1'
    redis_port = 6379
    redis_db = 1

    CACHES = {
        'default': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': f'redis://{redis_host}:{redis_port}/{redis_db}',
            'OPTIONS': {
                'CLIENT_CLASS': 'django_redis.client.DefaultClient',
                'SOCKET_KEEPALIVE': True
            }
        }
    }

    RQ_QUEUES = {
        'default': {
            'HOST': 'localhost',
            'PORT': 6379,
            'DB': 0,
            'DEFAULT_TIMEOUT': 3600,
            'WORKER_CLASS': 'oj_project.customrq.AutoReconnectWorker',
            'REDIS_CONNECTION_KWARGS': {
                'socket_connect_timeout': 5,
                'socket_timeout': 5,
                'retry_on_timeout': True,
            }
        },
        'high': {
            'HOST': 'localhost',
            'PORT': 6379,
            'DB': 0,
            'DEFAULT_TIMEOUT': 3600,
            'WORKER_CLASS': 'oj_project.customrq.AutoReconnectWorker',
            'REDIS_CONNECTION_KWARGS': {
                'socket_connect_timeout': 5,
                'socket_timeout': 5,
                'retry_on_timeout': True,
            }
        },
        'low': {
            'HOST': 'localhost',
            'PORT': 6379,
            'DB': 0,
            'DEFAULT_TIMEOUT': 3600,
            'WORKER_CLASS': 'oj_project.customrq.AutoReconnectWorker',
            'REDIS_CONNECTION_KWARGS': {
                'socket_connect_timeout': 5,
                'socket_timeout': 5,
                'retry_on_timeout': True,
            }
        },
    }

    # Multi-judge machine configuration
    # Each judge machine has its own RQ queue for distributed judging
    JUDGE_MACHINES = [
        {
            'name': 'judge-1',
            'host': '127.0.0.1',
            'port': 6379,
            'db': 0,
            'queue': 'judge-1',
            'enabled': True,
            'weight': 1,  # Load balancing weight
        },
        # Add more judge machines here:
        {
            'name': 'judge-2',
            'host': '192.168.3.117',
            'port': 6379,
            'db': 0,
            'queue': 'judge-2',
            'enabled': True,
            'weight': 1,
        },
    ]

    # Enable multi-judge mode
    OJ_MULTI_JUDGE_ENABLED = os.environ.get('OJ_MULTI_JUDGE_ENABLED', 'true').lower() in ('1', 'true', 'yes')

    # Dynamically add RQ queues for each judge machine
    for machine in JUDGE_MACHINES:
        if machine.get('enabled', True):
            RQ_QUEUES[machine['queue']] = {
                'HOST': machine['host'],
                'PORT': machine['port'],
                'DB': machine['db'],
                'DEFAULT_TIMEOUT': 3600,
                'WORKER_CLASS': 'oj_project.customrq.AutoReconnectWorker',
                'REDIS_CONNECTION_KWARGS': {
                    'socket_connect_timeout': 5,
                    'socket_timeout': 5,
                    'retry_on_timeout': True,
                }
            }

    RQ = {
        'exception_handler': 'django_rq.handlers.sentry',
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.filebased.FileBasedCache',
            'LOCATION': os.path.join(tempfile.gettempdir(), 'oj_demo_cache'),
        }
    }
    RQ_QUEUES = {}
    JUDGE_MACHINES = []
    OJ_MULTI_JUDGE_ENABLED = False

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
            'USER': os.environ.get('DB_USER', 'oscar.liu'),
            'PASSWORD': os.environ.get('DB_PASSWORD', '20131117Liu'),
            'HOST': os.environ.get('DB_HOST', '127.0.0.1'),
            'PORT': os.environ.get('DB_PORT', '5432'),
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

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
# MEDIA_URL = 'media/'
# MEDIA_ROOT = BASE_DIR / 'media'
# MEDIAFILES_DIRS = [BASE_DIR / 'media']

WHITENOISE_ROOT = BASE_DIR / 'static'
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = True

# Security settings for production
if not DEBUG:
    SECURE_HSTS_SECONDS = 31536000  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

AUTH_USER_MODEL = 'users.User'

CRISPY_ALLOWED_TEMPLATE_PACKS = "bootstrap5"

CRISPY_TEMPLATE_PACK = "bootstrap5"

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'home'
LOGOUT_REDIRECT_URL = 'home'

# Submission limits
OJ_MAX_SUBMISSION_CODE_BYTES = int(
    os.environ.get('OJ_MAX_SUBMISSION_CODE_BYTES', str(256 * 1024))
)

# Sandbox judge (Docker, no network inside container)
OJ_DOCKER_ENABLED = os.environ.get('OJ_DOCKER_ENABLED', 'true').lower() in ('1', 'true', 'yes')
OJ_DOCKER_IMAGE = os.environ.get('OJ_DOCKER_IMAGE', 'oj-judge:latest')
OJ_DOCKER_PIDS_LIMIT = int(os.environ.get('OJ_DOCKER_PIDS_LIMIT', '64'))

# Logging configuration
LOGGING = LOGGING
