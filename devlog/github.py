"""Fetch recent commits from the guwu-oj GitHub repository (cached)."""
import logging

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

REPO = 'alphadamn/guwu-oj'
BRANCH = 'main'
COMMITS_URL = f'https://api.github.com/repos/{REPO}/commits'
COMMITS_PAGE_URL = f'https://github.com/{REPO}/commits/{BRANCH}/'
CACHE_KEY = 'devlog_github_commits'
CACHE_TTL = 60 * 10  # 10 minutes


def get_commits(limit=15, force_refresh=False):
    """Return a list of recent commit dicts, cached for 10 minutes.

    Each item: {sha, short_sha, message, author, date, url}.
    Falls back gracefully (empty list) when GitHub is unreachable.
    """
    if not force_refresh:
        cached = cache.get(CACHE_KEY)
        if cached is not None:
            return cached

    commits = []
    try:
        resp = requests.get(
            COMMITS_URL,
            params={'sha': BRANCH, 'per_page': limit},
            headers={'Accept': 'application/vnd.github+json'},
            timeout=6,
        )
        resp.raise_for_status()
        for item in resp.json():
            commit = item.get('commit', {})
            author = commit.get('author', {}) or {}
            commits.append({
                'sha': item.get('sha', ''),
                'short_sha': (item.get('sha', '') or '')[:7],
                'message': (commit.get('message', '') or '').split('\n')[0],
                'author': author.get('name', ''),
                'date': author.get('date', ''),
                'url': item.get('html_url', ''),
            })
        cache.set(CACHE_KEY, commits, CACHE_TTL)
    except Exception as exc:  # noqa: BLE001 - never break the page on API errors
        logger.warning('Failed to fetch GitHub commits: %s', exc)
        # Cache the (empty) failure briefly so we don't hammer the API.
        cache.set(CACHE_KEY, commits, 60)

    return commits
