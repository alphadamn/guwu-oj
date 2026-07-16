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

import time
import random
from html.parser import HTMLParser
from urllib.parse import urlsplit

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
MAX_EXTERNAL_RESPONSE_BYTES = 1_000_000


def _external_get(url, *, allowed_hosts, session=None, **kwargs):
    """Request a fixed, allow-listed HTTPS search endpoint without redirects."""
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in allowed_hosts:
        raise ValueError('External search endpoint is not allow-listed')
    client = session or requests
    response = client.get(
        url,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=False,
        stream=True,
        **kwargs,
    )
    content_length = response.headers.get('Content-Length')
    if content_length and int(content_length) > MAX_EXTERNAL_RESPONSE_BYTES:
        response.close()
        raise ValueError('External search response is too large')
    body = bytearray()
    for chunk in response.iter_content(chunk_size=64 * 1024):
        body.extend(chunk)
        if len(body) > MAX_EXTERNAL_RESPONSE_BYTES:
            response.close()
            raise ValueError('External search response is too large')
    response._content = bytes(body)
    response._content_consumed = True
    return response


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


class _BingResultsParser(HTMLParser):
    """Extract Bing result cards using the HTML parser, not regex."""
    _VOID_ELEMENTS = {
        'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
        'meta', 'param', 'source', 'track', 'wbr',
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self._depth = 0
        self._current = None
        self._capture = None
        self._capture_depth = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self._current is None:
            classes = set((attrs.get('class') or '').split())
            if tag == 'li' and 'b_algo' in classes:
                self._current = {'title': [], 'url': '', 'snippet': []}
                self._depth = 1
            return

        if tag not in self._VOID_ELEMENTS:
            self._depth += 1
        classes = set((attrs.get('class') or '').split())
        if tag == 'a' and not self._current['url']:
            self._current['url'] = attrs.get('href', '')
            self._capture = 'title'
            self._capture_depth = self._depth
        elif (
            self._capture is None
            and (tag == 'p' or 'b_caption' in classes)
        ):
            self._capture = 'snippet'
            self._capture_depth = self._depth

    def handle_endtag(self, tag):
        if self._current is None or tag in self._VOID_ELEMENTS:
            return
        if self._capture is not None and self._depth == self._capture_depth:
            self._capture = None
            self._capture_depth = None
        self._depth -= 1
        if self._depth == 0:
            self.results.append(self._current)
            self._current = None
            self._capture = None
            self._capture_depth = None

    def handle_data(self, data):
        if self._current is not None and self._capture:
            self._current[self._capture].append(data)


def _html_to_text(text):
    parser = _TextExtractor()
    parser.feed(text or '')
    parser.close()
    return ' '.join(''.join(parser.parts).split())



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
        resp = _external_get(url, allowed_hosts={'api.duckduckgo.com'}, params=params)
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
        resp = _external_get(url, allowed_hosts={'en.wikipedia.org'}, params=params)
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
        resp = _external_get(
            url,
            allowed_hosts={'api.github.com'},
            params=params,
            headers={'Accept': 'application/vnd.github+json'},
        )
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
    """Parse HTML into text without regex-based tag stripping."""
    return _html_to_text(text)


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
        resp = _external_get(
            url, allowed_hosts={'www.bing.com'}, params=params, headers=headers
        )
        if resp.status_code != 200:
            return []
        page = resp.text
    except Exception:
        return []

    parser = _BingResultsParser()
    parser.feed(page)
    parser.close()
    results = []
    for item in parser.results:
        title = ' '.join(''.join(item['title']).split())
        url_i = item['url']
        if not title or not url_i:
            continue
        results.append({
            'title': title,
            'url': url_i,
            'snippet': ' '.join(''.join(item['snippet']).split())[:240],
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
            resp = _external_get(
                self.api_url,
                allowed_hosts={'so.csdn.net'},
                session=self.session,
                params=params,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
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
