"""Sliding time-window rate limiting.

Replaces fixed-window counters ("minute-per-minute" buckets that reset on
the clock boundary, allowing up to 2x the nominal rate across a boundary)
with true sliding windows: an event is counted if and only if it happened
within the trailing ``window_seconds``.

Primary backend: Redis sorted sets (one member per event, score = event
time in ms). Prune / count / add run atomically in a single Lua script,
so the check is race-free under concurrent workers.

Fallback backend: when no Redis client is available (e.g. the demo
file-based cache), a two-bucket weighted sliding-window counter is kept
through the generic Django cache API. It is an approximation (linear
weighting of the previous bucket) but never raises.

Public API
----------
- ``sliding_allow(key, limit, window_seconds)`` -> bool
      Record an event; return True while within budget, False once the
      limit has been reached inside the trailing window. Denied events
      are still recorded (so sustained abuse keeps the window full).
- ``sliding_add(key, window_seconds)`` -> int
      Unconditionally record an event; return the trailing-window count.
- ``sliding_count(key, window_seconds)`` -> int
      Count events in the trailing window without recording one.
- ``sliding_clear(key)`` -> None
      Forget every event for ``key``.
- ``sliding_ratelimit(...)``
      Drop-in replacement for ``django_ratelimit.decorators.ratelimit``
      backed by the sliding window. Sets ``request.limited`` and, with
      ``block=True``, raises ``PermissionDenied`` (handled by the site's
      403 view).
"""
from __future__ import annotations

import functools
import logging
import secrets
import time

from django.core.cache import cache

logger = logging.getLogger(__name__)

CACHE_PREFIX = 'sl'

# ---------------------------------------------------------------------------
# Redis backend (sorted-set sliding log)
# ---------------------------------------------------------------------------

# Atomically: prune members older than the window, then reject (or accept
# and record) the new event. Timestamps are milliseconds.
_ALLOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
local record_denied = ARGV[5] == '1'
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = tonumber(redis.call('ZCARD', key))
if count >= limit then
    if record_denied then
        redis.call('ZADD', key, now, member)
        redis.call('PEXPIRE', key, window + 1000)
    end
    return 0
end
redis.call('ZADD', key, now, member)
redis.call('PEXPIRE', key, window + 1000)
return 1
"""

_ADD_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local member = ARGV[3]
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
redis.call('ZADD', key, now, member)
redis.call('PEXPIRE', key, window + 1000)
return redis.call('ZCARD', key)
"""

_COUNT_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)
if tonumber(count) > 0 then
    redis.call('PEXPIRE', key, window + 1000)
end
return count
"""

_redis_client = None
_redis_checked = False


def _get_redis():
    """Return a raw Redis client for the default cache, or None.

    The result is cached after the first lookup; any failure (backend is
    not django-redis, Redis down) falls back to the cache-API path.
    """
    global _redis_client, _redis_checked
    if not _redis_checked:
        _redis_checked = True
        try:
            from django_redis import get_redis_connection
            client = get_redis_connection('default')
            client.ping()
            _redis_client = client
        except Exception as exc:
            logger.warning(
                'sliding-window: Redis unavailable (%s); using cache fallback',
                exc,
            )
            _redis_client = None
    return _redis_client


def _member(now_ms: int) -> str:
    return f'{now_ms}-{secrets.token_hex(8)}'


def _redis_key(key: str) -> str:
    """Map a logical cache key to the real Redis key.

    The raw django-redis client does NOT apply Django's key prefix /
    version (e.g. ``:1:``). Route through ``make_key`` so sliding-window
    keys live in the same namespace as regular cache keys — otherwise
    ``cache.delete()`` / ``cache.clear()`` could not touch them.
    """
    try:
        client = cache.client
        return client.make_key(key, version=getattr(client, 'version', None))
    except Exception:
        return key


# ---------------------------------------------------------------------------
# Fallback backend (two adjacent fixed buckets, linearly weighted)
# ---------------------------------------------------------------------------

def _fb_bucket_keys(key: str, window: int, now: int) -> tuple[str, str]:
    bucket = now // window
    return f'{key}:b{bucket}', f'{key}:b{bucket - 1}'


def _fb_count(key: str, window: int, now: int) -> float:
    cur_key, prev_key = _fb_bucket_keys(key, window, now)
    try:
        cur = int(cache.get(cur_key) or 0)
        prev = int(cache.get(prev_key) or 0)
    except Exception:
        return 0.0
    # Fraction of the previous bucket still covered by the trailing window.
    weight = (window - (now % window)) / float(window)
    return cur + prev * weight


def _fb_add(key: str, window: int, now: int) -> int:
    cur_key, _ = _fb_bucket_keys(key, window, now)
    ttl = window * 2 + 10
    try:
        if cache.add(cur_key, 1, timeout=ttl):
            return 1
        try:
            return int(cache.incr(cur_key))
        except (ValueError, TypeError, AttributeError):
            current = int(cache.get(cur_key) or 0) + 1
            cache.set(cur_key, current, timeout=ttl)
            return current
    except Exception:
        return int(_fb_count(key, window, now)) + 1


# ---------------------------------------------------------------------------
# Public primitives
# ---------------------------------------------------------------------------

def sliding_allow(key: str, limit: int, window_seconds: int, *,
                  record_denied: bool = True) -> bool:
    """Record an event against ``key`` and return True if within budget.

    ``limit`` events are permitted in any trailing ``window_seconds``.
    When the budget is exhausted the call returns False; by default the
    denied event is still logged so that a sustained flood cannot drain
    the window between retries.
    """
    limit = max(0, int(limit))
    window_ms = max(1, int(window_seconds)) * 1000
    now_ms = int(time.time() * 1000)
    try:
        client = _get_redis()
        if client is not None:
            script = client.register_script(_ALLOW_LUA)
            allowed = script(
                keys=[_redis_key(key)],
                args=[now_ms, window_ms, limit, _member(now_ms),
                      '1' if record_denied else '0'],
            )
            return bool(allowed)
    except Exception as exc:
        logger.warning('sliding-window allow failed for %s: %s', key, exc)

    # Cache-API fallback (approximate).
    window = max(1, int(window_seconds))
    now = int(time.time())
    allowed = _fb_count(key, window, now) < limit
    if allowed or record_denied:
        _fb_add(key, window, now)
    return allowed


def sliding_add(key: str, window_seconds: int) -> int:
    """Unconditionally record an event; return the trailing-window count."""
    window_ms = max(1, int(window_seconds)) * 1000
    now_ms = int(time.time() * 1000)
    try:
        client = _get_redis()
        if client is not None:
            script = client.register_script(_ADD_LUA)
            return int(script(keys=[_redis_key(key)],
                             args=[now_ms, window_ms, _member(now_ms)]))
    except Exception as exc:
        logger.warning('sliding-window add failed for %s: %s', key, exc)

    window = max(1, int(window_seconds))
    return _fb_add(key, window, int(time.time()))


def sliding_count(key: str, window_seconds: int) -> int:
    """Return the number of events recorded in the trailing window."""
    window_ms = max(1, int(window_seconds)) * 1000
    now_ms = int(time.time() * 1000)
    try:
        client = _get_redis()
        if client is not None:
            script = client.register_script(_COUNT_LUA)
            return int(script(keys=[_redis_key(key)], args=[now_ms, window_ms]))
    except Exception as exc:
        logger.warning('sliding-window count failed for %s: %s', key, exc)

    window = max(1, int(window_seconds))
    return int(_fb_count(key, window, int(time.time())))


def sliding_clear(key: str) -> None:
    """Forget all events recorded against ``key``."""
    try:
        client = _get_redis()
        if client is not None:
            client.delete(_redis_key(key))
            return
    except Exception as exc:
        logger.warning('sliding-window clear failed for %s: %s', key, exc)

    # Bucket geometry is unknown here; clear current + previous buckets
    # for every window size the project actually uses.
    now = int(time.time())
    for bucket_window in (1, 60, 300, 600, 3600, 86400):
        cur_key, prev_key = _fb_bucket_keys(key, bucket_window, now)
        for k in (cur_key, prev_key):
            try:
                cache.delete(k)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# View decorator (drop-in for django-ratelimit's @ratelimit)
# ---------------------------------------------------------------------------

_RATE_UNITS = {
    's': 1,
    'm': 60,
    'h': 3600,
    'd': 86400,
}


def _parse_rate(rate) -> tuple[int, int]:
    """Parse ``'5/m'`` style rates into ``(limit, window_seconds)``."""
    if callable(rate):
        rate = rate()
    if rate is None:
        return 0, 60  # "disabled" semantics: limit 0 -> everything limited? no.
    if isinstance(rate, (int, float)):
        return int(rate), 60
    text = str(rate).strip()
    try:
        num, unit = text.split('/')
        num = int(num)
        if len(unit) != 1 or unit not in _RATE_UNITS:
            raise ValueError(unit)
        return num, _RATE_UNITS[unit]
    except (ValueError, AttributeError) as exc:
        raise ValueError(f'Invalid rate {rate!r}; expected e.g. "5/m"') from exc


def _ratelimit_key(group: str, request, key) -> str | None:
    """Build the per-client discriminator, mirroring django-ratelimit.

    Returns None when no discriminator can be derived (e.g. anonymous
    request with ``key='user'``); such requests are not limited, matching
    django-ratelimit behavior.
    """
    if callable(key):
        value = key(group, request)
        return None if value is None else str(value)
    if key == 'ip':
        from .captcha import _client_ip
        return f'ip:{_client_ip(request)}'
    if key == 'user':
        user = getattr(request, 'user', None)
        if user is None or not getattr(user, 'is_authenticated', False):
            return None
        return f'user:{getattr(user, "pk", "")}'
    raise ValueError(f'Unsupported ratelimit key: {key!r}')


def sliding_ratelimit(group: str | None = None, key='ip', rate='5/m',
                      method='POST', block: bool = False):
    """Decorator enforcing a sliding-window rate limit on a view.

    Compatible with ``django_ratelimit.decorators.ratelimit`` for the
    options used in this project: ``key`` ('ip'/'user'/callable),
    ``rate`` ('N/s|m|h|d' or callable), ``method`` (string or iterable),
    ``block``. Sets ``request.limited``; with ``block=True`` raises
    ``PermissionDenied`` (rendered by the site 403 handler).
    """
    if isinstance(method, str):
        methods = {method.upper()}
    else:
        methods = {m.upper() for m in method}

    def decorator(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if getattr(request, 'method', '').upper() not in methods:
                return view(request, *args, **kwargs)

            request.limited = False
            try:
                limit, window = _parse_rate(rate)
                grp = group or f'{view.__module__}.{view.__qualname__}'
                discriminator = _ratelimit_key(grp, request, key)
                if discriminator is not None and limit > 0:
                    rl_key = f'{CACHE_PREFIX}:rl:{grp}:{discriminator}'
                    if not sliding_allow(rl_key, limit, window):
                        request.limited = True
                        if block:
                            from django.core.exceptions import PermissionDenied
                            raise PermissionDenied('Rate limit exceeded')
            except PermissionDenied:
                raise
            except Exception as exc:
                # Fail open: a cache outage must not take the views down.
                logger.warning('sliding-ratelimit decorator failed: %s', exc)

            return view(request, *args, **kwargs)

        return wrapper

    return decorator
