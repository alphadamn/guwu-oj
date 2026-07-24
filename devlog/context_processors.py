"""Django template context processor that injects OJ-wide settings.

Exposes:
- ``OJ_MONACO_BASE``: active Monaco ``/min/vs`` URL prefix.
- ``OJ_BOOTSTRAP_CSS`` / ``OJ_BOOTSTRAP_ICONS`` / ``OJ_BOOTSTRAP_JS``.
- ``OJ_STATIC_CACHE_TTL``: seconds for ``/static/`` Cache-Control.
"""


_DEFAULT_MONACO_BASE = 'https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min/vs'
_DEFAULT_BOOTSTRAP = (
    'https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css',
    'https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.0/font/bootstrap-icons.css',
    'https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js',
)
_DEFAULT_STATIC_TTL = 86400


def oj_site(request):
    try:
        from devlog.models import SiteConfig
        monaco_base = SiteConfig.monaco_base()
        bootstrap_urls = SiteConfig.bootstrap_urls()
        static_ttl = SiteConfig.static_cache_ttl()
        browser_geolocation_enabled = SiteConfig.browser_geolocation_is_enabled()
    except Exception:
        monaco_base = _DEFAULT_MONACO_BASE
        bootstrap_urls = _DEFAULT_BOOTSTRAP
        static_ttl = _DEFAULT_STATIC_TTL
        browser_geolocation_enabled = True

    if not bootstrap_urls or len(bootstrap_urls) < 3:
        bootstrap_urls = _DEFAULT_BOOTSTRAP

    return {
        'OJ_MONACO_BASE': monaco_base,
        'OJ_BOOTSTRAP_CSS': bootstrap_urls[0],
        'OJ_BOOTSTRAP_ICONS': bootstrap_urls[1],
        'OJ_BOOTSTRAP_JS': bootstrap_urls[2],
        'OJ_STATIC_CACHE_TTL': int(static_ttl or _DEFAULT_STATIC_TTL),
        'OJ_BROWSER_GEOLOCATION_ENABLED': browser_geolocation_enabled,
    }
