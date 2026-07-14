"""Django middlewares used by the OJ frontend.

- ``StaticCacheHeaders``: Re-writes the ``Cache-Control`` header on
  ``/static/`` responses so the value is controlled by
  ``SiteConfig.static_cache_ttl_seconds`` (default 1 day).  Files whose
  URL contains a hash produced by ``ManifestStaticFilesStorage`` get
  immutable / 1-year headers — they can be cached forever.
"""

from __future__ import annotations

import os
import re
import threading

_CACHE_LOCK = threading.Lock()
_CACHE_TTL = None  # type: int | None
_CACHE_AT = 0.0  # unix time last read


def _read_ttl() -> int:
    """Read ``SiteConfig.static_cache_ttl`` with a 30s in-process cache."""
    import time
    global _CACHE_TTL, _CACHE_AT
    now = time.time()
    if _CACHE_TTL is not None and now - _CACHE_AT < 30:
        return _CACHE_TTL
    with _CACHE_LOCK:
        if _CACHE_TTL is not None and time.time() - _CACHE_AT < 30:
            return _CACHE_TTL
        try:
            from devlog.models import SiteConfig
            ttl = int(SiteConfig.static_cache_ttl())
        except Exception:
            ttl = 86400
        _CACHE_TTL = ttl
        _CACHE_AT = time.time()
        return ttl


# Detect ManifestStaticFilesStorage hashes: 12+ hex digits between dots.
_MANIFEST_HASH_RE = re.compile(r'\.[0-9a-f]{12,}\.')


class StaticCacheHeaders:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # Only touch paths that look like static files.
        path = getattr(request, 'path', '') or ''
        if path.startswith('/static/') or path == '/static' or path.startswith('static/'):
            filename = os.path.basename(path)
            if _MANIFEST_HASH_RE.search(filename) or '.min.' in filename:
                immutable = True
            else:
                immutable = False

            ttl = _read_ttl()
            if immutable:
                # Manifest-hashed files never change path; allow 1 year cache.
                max_age = 31536000
                response['Cache-Control'] = (
                    f'public, max-age={max_age}, stale-while-revalidate=86400, '
                    f'stale-if-error=86400, immutable'
                )
            else:
                max_age = max(0, int(ttl))
                response['Cache-Control'] = (
                    f'public, max-age={max_age}, stale-while-revalidate=3600, '
                    f'stale-if-error=3600'
                )
            # Ensure we also set a matching Expires fallback for old clients.
            try:
                import email.utils, time
                response['Expires'] = email.utils.formatdate(
                    time.time() + max_age, usegmt=True
                )
            except Exception:
                pass
        return response
