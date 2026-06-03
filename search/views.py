import hashlib

from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string

from .services import EXTERNAL_BACKENDS, LOCAL_BACKENDS, run_search


CACHE_TTL = 60 * 15  # 15 minutes
FREQ_KEY = 'search:query:frequency'  # Redis sorted set
TOP_QUERIES_LIMIT = 10


def _cache_key(query, active_sources):
    sources = sorted(active_sources)
    raw = f'{query.lower().strip()}|{",".join(sources)}'
    digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    return f'search:results:{digest}'


def _bump_query_frequency(query):
    """Increment the query's score in a Redis sorted set (no-op if Redis unavailable)."""
    if not query:
        return
    try:
        client = cache.client.get_client(write=True) if hasattr(cache, 'client') else None
        if client is None:
            return
        client.zincrby(FREQ_KEY, 1, query)
    except Exception:
        pass


def _top_queries(limit=TOP_QUERIES_LIMIT):
    try:
        client = cache.client.get_client(write=False) if hasattr(cache, 'client') else None
        if client is None:
            return []
        items = client.zrevrange(FREQ_KEY, 0, limit - 1, withscores=True)
        out = []
        for member, score in items:
            if isinstance(member, bytes):
                member = member.decode('utf-8', errors='ignore')
            out.append({'q': member, 'score': int(score)})
        return out
    except Exception:
        return []


def _available_sources():
    return [name for name, _ in LOCAL_BACKENDS] + [name for name, _ in EXTERNAL_BACKENDS]


def _normalize_sources(selected):
    available = set(_available_sources())
    if not selected:
        return available
    return {s for s in selected if s in available}


def _run_and_group(query, active_sources):
    """Run search, apply Redis caching, and build template-ready groups."""
    raw_results = {}
    from_cache = False
    if query:
        key = _cache_key(query, active_sources)
        cached = cache.get(key)
        if cached is not None:
            raw_results = cached
            from_cache = True
        else:
            raw_results = run_search(query, sources=active_sources)
            try:
                cache.set(key, raw_results, CACHE_TTL)
            except Exception:
                pass
        _bump_query_frequency(query)

    def build_group(name, is_local):
        items = raw_results.get(name, [])
        return {
            'name': name,
            'items': items,
            'error': raw_results.get(f'__error_{name}'),
            'icon': 'bi-database' if is_local else 'bi-globe',
            'kind': 'local' if is_local else 'web',
        }

    groups = []
    for is_local in (True, False):
        backends = LOCAL_BACKENDS if is_local else EXTERNAL_BACKENDS
        for name, _ in backends:
            groups.append(build_group(name, is_local))
    total = sum(len(g['items']) for g in groups)
    return {
        'groups': groups,
        'total': total,
        'from_cache': from_cache,
        'top_queries': _top_queries(),
    }


def search_view(request):
    """Render the search shell immediately. Results are loaded via /search/results/."""
    query = request.GET.get('q', '').strip()
    selected = request.GET.getlist('src')
    active_sources = _normalize_sources(selected)

    return render(request, 'search/search.html', {
        'query': query,
        'available_sources': _available_sources(),
        'active_sources': active_sources,
        'top_queries': _top_queries(),
    })


def search_results_api(request):
    """JSON endpoint returning rendered search result HTML + metadata."""
    query = request.GET.get('q', '').strip()
    selected = request.GET.getlist('src')
    active_sources = _normalize_sources(selected)

    payload = _run_and_group(query, active_sources)
    html = render_to_string('search/_results_groups.html', {
        'query': query,
        'groups': payload['groups'],
        'total': payload['total'],
    }, request=request) if query else ''

    return JsonResponse({
        'query': query,
        'total': payload['total'],
        'from_cache': payload['from_cache'],
        'top_queries': payload['top_queries'],
        'html': html,
    })
