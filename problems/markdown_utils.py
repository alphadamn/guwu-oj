import re
import hashlib
import html

import markdown
import bleach
from django.core.cache import cache

ALLOWED_TAGS = bleach.sanitizer.ALLOWED_TAGS | frozenset({
    'p', 'pre', 'code', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'table', 'thead', 'tbody', 'tr', 'th', 'td', 'hr', 'br',
    'img', 'blockquote', 'span', 'div', 'del', 's', 'strike',
})

ALLOWED_ATTRIBUTES = {
    **bleach.sanitizer.ALLOWED_ATTRIBUTES,
    'a': ['href', 'title', 'rel'],
    'img': ['src', 'alt', 'title'],
    'code': ['class'],
    'span': ['class'],
}

_DISPLAY_MATH_PATTERNS = [
    re.compile(r'\$\$[\s\S]+?\$\$', re.MULTILINE),
    re.compile(r'\\\[[\s\S]+?\\\]', re.MULTILINE),
]
# The \(...\) branch uses a tempered greedy token ((?:(?!\\\)).)*?) instead
# of (.+?) so the content cannot backtrack into the closing \) delimiter,
# removing the ambiguity that previously allowed polynomial-time matching.
_INLINE_MATH_RE = re.compile(
    r'(?<!\$)\$(?!\$)([^\$\n]+?)\$(?!\$)|'
    r'\\\(((?:(?!\\\)).)*?)\\\)',
)


def _protect_math(text):
    storage = []

    def stash(match):
        # 关键修复：存储转义后的内容，而不是原始内容
        storage.append(html.escape(match.group(0)))
        return f'[[MATH{len(storage) - 1}]]'

    for pattern in _DISPLAY_MATH_PATTERNS:
        text = pattern.sub(stash, text)
    text = _INLINE_MATH_RE.sub(stash, text)
    return text, storage


def _restore_math(html, storage):
    for i, block in enumerate(storage):
        html = html.replace(f'[[MATH{i}]]', block)
    return html


_STRIKETHROUGH_RE = re.compile(r'~~(.+?)~~')


def _apply_strikethrough(text):
    """将 ~~text~~ 转换为 <del>text</del>。在代码块外执行替换。"""
    # 先保护代码块，避免 ~~ 在 ``` 中被替换
    parts = re.split(r'(```[\s\S]*?```|`[^`\n]+`)', text)
    for i, part in enumerate(parts):
        if i % 2 == 0:  # 偶数索引是非代码部分
            parts[i] = _STRIKETHROUGH_RE.sub(r'<del>\1</del>', part)
    return ''.join(parts)


def render_markdown(text):
    if not text:
        return ''

    # Generate cache key based on content hash
    content_hash = hashlib.md5(text.encode('utf-8'), usedforsecurity=False).hexdigest()
    cache_key = f'markdown_render_{content_hash}'

    # Try to get cached result
    cached_html = cache.get(cache_key)
    if cached_html is not None:
        return cached_html

    # Cap the input length passed to the math-extraction regexes. The
    # non-greedy quantifiers in _protect_math can require O(n^2) time on
    # pathological inputs (e.g. many unclosed \( sequences), so bounding n
    # keeps the worst-case runtime small. Real markdown with math is far
    # below this cap (the largest handbook article is ~3.6 KB), so this
    # branch is rarely hit in practice.
    _MAX_MATH_INPUT = 10_000
    if len(text) <= _MAX_MATH_INPUT:
        text, math_blocks = _protect_math(text)
    else:
        math_blocks = []

    text = _apply_strikethrough(text)
    html = markdown.markdown(
        text,
        extensions=['extra', 'fenced_code', 'tables', 'nl2br', 'sane_lists'],
        extension_configs={'fenced_code': {'lang_prefix': 'language-'}},
    )
    html = bleach.linkify(
        bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES),
        parse_email=False,
    )
    html = _restore_math(html, math_blocks)
    
    # Cache the result for 1 hour
    cache.set(cache_key, html, 60 * 60)
    
    return html
