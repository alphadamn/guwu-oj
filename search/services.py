"""Search backends aggregated by the built‑in search engine.

Each backend exposes ``search(query) -> list[dict]``. Result dicts use a
common shape:

{
    "title": str,
    "url": str,
    "snippet": str,
    "source": str,        # display name of the backend
    "type": str,          # optional category label (local/web/docs/...)
}
"""

import re
import html
import csv
import time
import random
from urllib.parse import quote

import requests
from django.db.models import Q

try:
    from problems.models import Problem, Solution
except Exception:  # pragma: no cover - import guard during scaffolding
    Problem = None
    Solution = None

try:
    from handbook.content import HANDBOOK_CATEGORIES
except Exception:  # pragma: no cover
    HANDBOOK_CATEGORIES = {}


REQUEST_TIMEOUT = 4  # seconds, applied to all external HTTP calls


# --------------------------------------------------------------------------- #
# Local backends
# --------------------------------------------------------------------------- #

def search_problems(query):
    if Problem is None:
        return []
    qs = Problem.objects.filter(
        Q(title__icontains=query)
        | Q(description__icontains=query)
        | Q(tags__icontains=query)
    ).filter(is_public=True)
    return [
        {
            'title': f'P{p.id} – {p.title}',
            'url': f'/problem/{p.id}/',
            'snippet': (p.description or '')[:200],
            'source': '题库',
            'type': 'local',
        }
        for p in qs
    ]


def search_solutions(query):
    if Solution is None:
        return []
    qs = Solution.objects.filter(
        Q(title__icontains=query) | Q(content__icontains=query)
    ).filter(is_approved=True)
    return [
        {
            'title': s.title,
            'url': f'/problem/{s.problem_id}/solution/{s.id}/',
            'snippet': (s.content or '')[:200],
            'source': '题解',
            'type': 'local',
        }
        for s in qs
    ]


def search_handbook(query):
    """Search the in‑repo handbook content."""
    results = []
    q = query.lower().split()
    for cat_slug, cat in HANDBOOK_CATEGORIES.items():
        for slug, article in cat.get('articles', {}).items():
            haystack = ' '.join([
                article.get('title', ''),
                article.get('summary', ''),
                article.get('content', ''),
            ]).lower()
            # if q in haystack:
            cnt = 0
            for i in q:
                if i in haystack:
                    cnt += 1

            if cnt > 4:
                results.append({
                    'title': article.get('title', slug),
                    'url': f'/handbook/{cat_slug}/{slug}/',
                    'snippet': article.get('summary', '') or (article.get('content', '')[:200]),
                    'source': f'手册 · {cat.get("title", cat_slug)}',
                    'type': 'docs',
                })
    return results


# --------------------------------------------------------------------------- #
# External backends
# --------------------------------------------------------------------------- #

def search_ddg(query):
    """DuckDuckGo Instant Answer API – abstract + related topics."""
    url = 'https://api.duckduckgo.com/'
    params = {'q': query, 'format': 'json', 'no_html': '1', 'skip_disambig': '1'}
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []

    out = []
    if data.get('AbstractText'):
        out.append({
            'title': data.get('Heading') or query,
            'url': data.get('AbstractURL') or '',
            'snippet': data['AbstractText'],
            'source': 'DuckDuckGo',
            'type': 'web',
        })

    for topic in (data.get('RelatedTopics') or []):
        if isinstance(topic, dict) and topic.get('Text'):
            out.append({
                'title': topic.get('Text', '').split(' - ')[0],
                'url': topic.get('FirstURL', ''),
                'snippet': topic.get('Text', ''),
                'source': 'DuckDuckGo',
                'type': 'web',
            })
    return out


def search_wikipedia(query):
    """Wikipedia OpenSearch API – title + snippet pairs."""
    url = 'https://en.wikipedia.org/w/api.php'
    params = {
        'action': 'opensearch',
        'search': query,
        'limit': '50',
        'namespace': '0',
        'format': 'json',
        'origin': '*',
    }
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []
    if len(data) < 4:
        return []
    titles, snippets, links = data[1], data[2], data[3]
    return [
        {
            'title': titles[i],
            'url': links[i],
            'snippet': snippets[i] or titles[i],
            'source': 'Wikipedia',
            'type': 'docs',
        }
        for i in range(len(titles))
        if links[i]
    ]


def search_github(query):
    """Search GitHub repositories (no auth; subject to rate limits)."""
    url = 'https://api.github.com/search/repositories'
    params = {'q': query, 'per_page': '30', 'sort': 'stars'}
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT,
                            headers={'Accept': 'application/vnd.github+json'})
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []
    items = data.get('items', [])[:10]
    return [
        {
            'title': f"{repo['full_name']} ★{repo['stargazers_count']}",
            'url': repo['html_url'],
            'snippet': (repo.get('description') or '')[:240],
            'source': 'GitHub',
            'type': 'code',
        }
        for repo in items
    ]


def _strip_tags(text):
    """Remove HTML tags and collapse whitespace."""
    if not text:
        return ''
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


def search_bing(query):
    """Scrape Bing web search results (HTML, no API key required)."""
    url = 'https://www.bing.com/search'
    params = {'q': query, 'count': '50'}
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'
        ),
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    }
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT, headers=headers)
        if resp.status_code != 200:
            return []
        page = resp.text
    except Exception:
        return []

    # Each organic result lives in <li class="b_algo"> ... </li>
    blocks = re.findall(r'<li[^>]*class="[^"]*\bb_algo\b[^"]*"[^>]*>(.*?)</li>', page, flags=re.S)
    results = []
    for block in blocks:
        # Prefer the link inside <h2> – this is the canonical title link.
        h2 = re.search(r'<h2[^>]*>(.*?)</h2>', block, flags=re.S)
        scope = h2.group(1) if h2 else block
        link = re.search(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', scope, flags=re.S)
        if not link:
            continue

        url_i = html.unescape(link.group(1))
        # Strip <cite>…</cite> from the anchor's inner HTML before stripping
        # tags – Bing sometimes embeds the visible URL (e.g. "example.com")
        # plus the full URL inside <cite>, which would otherwise leak into
        # the title as "example.comhttps://example.com/...".
        anchor_html = link.group(2)
        anchor_html = re.sub(r'<cite\b[^>]*>.*?</cite>', '', anchor_html, flags=re.S | re.I)
        title = _strip_tags(anchor_html) or _strip_tags(h2.group(1) if h2 else '')

        snippet_match = re.search(
            r'<p[^>]*>(.*?)</p>', block, flags=re.S
        ) or re.search(r'<div[^>]*class="[^"]*\bb_caption\b[^"]*"[^>]*>(.*?)</div>', block, flags=re.S)
        snippet = _strip_tags(snippet_match.group(1)) if snippet_match else ''

        if not title or not url_i:
            continue
        results.append({
            'title': title,
            'url': url_i,
            'snippet': snippet[:240],
            'source': 'Bing',
            'type': 'web',
        })
    return results


class CSDNSearchAPI:
    def __init__(self, keyword, max_pages=2):
        self.keyword = keyword
        self.max_pages = max_pages
        self.api_url = "https://so.csdn.net/api/v3/search"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Referer': 'https://so.csdn.net/',
            'Accept': 'application/json, text/plain, */*',
        })

    def fetch_page(self, page):
        params = {'q': self.keyword, 'p': page}
        try:
            resp = self.session.get(self.api_url, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            # print(f"API请求失败: {e}")
            return None

    def parse_data(self, json_data, page):
        if not json_data or 'result_vos' not in json_data:
            return []
        items = json_data['result_vos']
        results = []
        for item in items:
            results.append({
                'title': item.get('title', ''),
                'link': item.get('url', ''),
                'author': item.get('author', ''),
                'date': item.get('publishTime', ''),
                'views': str(item.get('viewCount', '')),
                'description': item.get('description', ''),
                'keyword': self.keyword,
                'page': page
            })
        return results

    def crawl(self):
        all_results = []
        for page in range(1, self.max_pages + 1):
            # print(f"请求第 {page} 页...")
            json_data = self.fetch_page(page)
            if not json_data:
                break
            page_results = self.parse_data(json_data, page)
            if not page_results:
                # print("无更多数据")
                break
            # print(f"第 {page} 页获取 {len(page_results)} 条")
            all_results.extend(page_results)
            time.sleep(random.uniform(1, 2))
        return all_results

def search_csdn(query):
    """
    CSDN search backend compatible with `run_search`.

    `CSDNSearchAPI.crawl()` returns raw items shaped like:
        {title, link, author, date, views, description, keyword, page}
    We normalize them to the common result shape used by the aggregator:
        {title, url, snippet, source, type}
    """
    crawler = CSDNSearchAPI(query, max_pages=2)
    raw_items = crawler.crawl()

    normalized = []
    seen_urls = set()
    for item in raw_items:
        url_i = item.get('link') or ''
        title = _strip_tags(item.get('title') or '')
        description = _strip_tags(item.get('description') or '')

        if not title or not url_i or url_i in seen_urls:
            continue
        seen_urls.add(url_i)

        meta_bits = []
        author = item.get('author')
        views = str(item.get('views') or '').strip()
        date = item.get('date')
        if author:
            meta_bits.append(str(author))
        if views:
            meta_bits.append(f'阅读 {views}')
        if date:
            meta_bits.append(str(date))
        meta = ' · '.join(meta_bits)
        snippet = (meta + (' — ' + description if description else '')).strip()

        normalized.append({
            'title': title,
            'url': str(url_i[:50])+'...' if len(url_i) > 50 else url_i,
            'snippet': snippet[:240],
            'source': 'CSDN',
            'type': 'blog',
        })
    # print(normalized)
    return normalized


# --------------------------------------------------------------------------- #
# Aggregator
# --------------------------------------------------------------------------- #

LOCAL_BACKENDS = [
    ('题库', search_problems),
    ('题解', search_solutions),
    ('手册', search_handbook),
]

EXTERNAL_BACKENDS = [
    ('DuckDuckGo', search_ddg),
    ('Wikipedia', search_wikipedia),
    ('CSDN', search_csdn),
    ('GitHub', search_github),
    ('Bing', search_bing),
]


def _canonical_url(url):
    """Normalize a URL for cross-source deduplication.

    Strips trailing slashes, lower-cases the host, and drops noise parameters
    that often differ between search engines (utm_*, refs, tracking tokens).
    Two URLs that point to the same page should canonicalize to the same key.
    """
    if not url:
        return ''
    from urllib.parse import urlsplit, parse_qsl, urlencode

    try:
        parts = urlsplit(url.strip())
    except Exception:
        return url.strip()

    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path
    if path.endswith('/') and len(path) > 1:
        path = path.rstrip('/')

    # Keep only stable query params; drop utm/ref/tracking junk.
    keep = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        kl = k.lower()
        if kl.startswith('utm_'):
            continue
        if kl in {'spm', 'scm', 'from', 'isappinstalled', 'share_source',
                  'vd_source', 'request_id', 'biz_id', 'ops_request_misc'}:
            continue
        keep.append((k, v))
    query = urlencode(keep)
    fragment = ''  # ignore fragment; #same-page anchors shouldn't split dupes

    return urlsplit('')._replace(
        scheme=scheme, netloc=netloc, path=path, query=query, fragment=fragment
    ).geturl()


def run_search(query, sources=None):
    """Run all enabled backends and return a dict keyed by backend name.

    Results are de-duplicated across sources: each canonical URL is kept only
    in the first backend that returned it, and dropped from later backends.
    """
    enabled_locals = LOCAL_BACKENDS
    enabled_externals = EXTERNAL_BACKENDS
    if sources:
        enabled_locals = [(n, fn) for n, fn in LOCAL_BACKENDS if n in sources]
        enabled_externals = [(n, fn) for n, fn in EXTERNAL_BACKENDS if n in sources]

    # print(enabled_externals)

    grouped = {}
    seen_urls = set()
    for name, fn in list(enabled_locals) + list(enabled_externals):
        try:
            items = fn(query)
        except Exception as exc:  # never let one backend break the page
            grouped[name] = []
            grouped[f'__error_{name}'] = str(exc)
            continue

        kept = []
        for item in items:
            url = item.get('url') or ''
            key = _canonical_url(url)
            if not key or key in seen_urls:
                continue
            seen_urls.add(key)
            kept.append(item)
        grouped[name] = kept
    return grouped
