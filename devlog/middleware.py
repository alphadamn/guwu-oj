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
import uuid

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


class TrafficMetricsMiddleware:
    """Count successful public HTML page views without storing visitor data."""

    EXCLUDED_PREFIXES = (
        '/admin/', '/static/', '/media/', '/health/', '/metrics', '/devlog/', '/rq/',
    )
    ANALYTICS_COOKIE = 'oj_analytics_id'
    CONSENT_COOKIE = 'oj_analytics_consent'
    RETENTION_DAYS = 90

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if self._should_record(request, response):
            self._record_page_view(request)
            self._record_user_view(request, response)
        return response

    def _should_record(self, request, response):
        path = getattr(request, 'path', '') or ''
        content_type = response.headers.get('Content-Type', '')
        return (
            request.method in ('GET', 'HEAD')
            and 200 <= response.status_code < 400
            and 'text/html' in content_type
            and not any(path.startswith(prefix) for prefix in self.EXCLUDED_PREFIXES)
        )

    def _normalized_path(self, request):
        path = (getattr(request, 'path', '') or '/').rstrip('/') or '/'
        segments = path.strip('/').split('/')
        normalized = []
        for segment in segments:
            normalized.append(':id' if segment.isdigit() else segment)
        return '/' + '/'.join(normalized) + ('/' if path != '/' else '')

    def _record_page_view(self, request):
        from datetime import timedelta

        from django.db import IntegrityError, transaction
        from django.db.models import F
        from django.utils import timezone

        from devlog.models import TrafficCountryMetric, TrafficDailyMetric, TrafficPageMetric

        today = timezone.localdate()
        updated = TrafficDailyMetric.objects.filter(day=today).update(
            page_views=F('page_views') + 1
        )
        created = False
        if not updated:
            try:
                with transaction.atomic():
                    TrafficDailyMetric.objects.create(day=today, page_views=1)
                    created = True
            except IntegrityError:
                TrafficDailyMetric.objects.filter(day=today).update(
                    page_views=F('page_views') + 1
                )
        normalized_path = self._normalized_path(request)
        page_updated = TrafficPageMetric.objects.filter(
            day=today, path=normalized_path
        ).update(page_views=F('page_views') + 1)
        if not page_updated:
            try:
                with transaction.atomic():
                    TrafficPageMetric.objects.create(
                        day=today, path=normalized_path, page_views=1
                    )
            except IntegrityError:
                TrafficPageMetric.objects.filter(
                    day=today, path=normalized_path
                ).update(page_views=F('page_views') + 1)

        country = None
        try:
            from devlog.geoip import country_for_request
            country = country_for_request(request)
        except Exception:
            country = None
        if country:
            country_updated = TrafficCountryMetric.objects.filter(
                day=today, country_code=country['country_code']
            ).update(requests=F('requests') + 1)
            if not country_updated:
                try:
                    with transaction.atomic():
                        TrafficCountryMetric.objects.create(day=today, requests=1, **country)
                except IntegrityError:
                    TrafficCountryMetric.objects.filter(
                        day=today, country_code=country['country_code']
                    ).update(requests=F('requests') + 1)

        if created:
            cutoff = today - timedelta(days=self.RETENTION_DAYS)
            TrafficDailyMetric.objects.filter(day__lt=cutoff).delete()
            TrafficPageMetric.objects.filter(day__lt=cutoff).delete()
            TrafficCountryMetric.objects.filter(day__lt=cutoff).delete()
    def _record_user_view(self, request, response):
        """Record consented route counts without storing IPs or visit timestamps."""
        if request.COOKIES.get(self.CONSENT_COOKIE) != 'accepted':
            return
        from datetime import timedelta
        from django.db import IntegrityError, transaction
        from django.db.models import F
        from django.utils import timezone
        from devlog.models import UserTrafficMetric

        visitor_id = request.COOKIES.get(self.ANALYTICS_COOKIE)
        user = request.user if getattr(request.user, 'is_authenticated', False) else None
        if not user and not visitor_id:
            visitor_id = uuid.uuid4().hex
            response.set_cookie(self.ANALYTICS_COOKIE, visitor_id, max_age=31536000, samesite='Lax')
        if not user and not visitor_id:
            return
        hour = timezone.localtime().replace(minute=0, second=0, microsecond=0)
        path = self._normalized_path(request)
        try:
            with transaction.atomic():
                if user:
                    for row in UserTrafficMetric.objects.filter(session_key=visitor_id, user__isnull=True):
                        target, merged = UserTrafficMetric.objects.get_or_create(
                            user=user, hour=row.hour, path=row.path,
                            defaults={'page_views': row.page_views},
                        )
                        if not merged:
                            UserTrafficMetric.objects.filter(pk=target.pk).update(
                                page_views=F('page_views') + row.page_views,
                            )
                    UserTrafficMetric.objects.filter(session_key=visitor_id, user__isnull=True).delete()
                    lookup = {'user': user, 'hour': hour, 'path': path}
                else:
                    lookup = {'session_key': visitor_id, 'hour': hour, 'path': path}
                metric, created = UserTrafficMetric.objects.get_or_create(
                    defaults={'page_views': 1}, **lookup,
                )
                if not created:
                    UserTrafficMetric.objects.filter(pk=metric.pk).update(
                        page_views=F('page_views') + 1,
                    )
            cutoff = hour - timedelta(days=self.RETENTION_DAYS)
            UserTrafficMetric.objects.filter(hour__lt=cutoff).delete()
        except (IntegrityError, ValueError):
            return


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
