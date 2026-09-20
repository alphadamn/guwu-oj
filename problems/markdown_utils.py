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

# Math extraction is performed by a hand-written linear (O(n)) scanner
# rather than regular expressions. A regex with lazy quantifiers (even a
# tempered greedy token) is retried by the regex engine at every possible
# start position on an unclosed opener; with m unmatched openers each
# failed attempt scans toward the end of the string, giving O(m*n)=O(n^2)
# behaviour -- many "\(" sequences made re.sub take seconds and was
# reported as py/polynomial-redos. The scanner below advances a set of
# monotonic finders; every byte of input is examined a constant number of
# times, so extraction cannot blow up on any input.


def _placeholder(storage, block):
    # 关键修复：存储转义后的内容，而不是原始内容
    storage.append(html.escape(block))
    return f'[[MATH{len(storage) - 1}]]'


def _display_sweep(text, opener, close, storage):
    """Extract non-empty opener...close blocks, e.g. $$...$$ or \\[...\\].

    The body may cross newlines (matches [\\s\\S]+?). Candidate closes are
    tried left to right; an empty-bodied candidate is consumed one
    character at a time and the search overlaps (j+1). At most one empty
    candidate exists per opener, so this is linear.
    """
    parts = []
    pos = 0
    while True:
        i = text.find(opener, pos)
        if i < 0:
            parts.append(text[pos:])
            break
        cpos = i + len(opener)
        while True:
            j = text.find(close, cpos)
            if j < 0:
                # Unclosed opener: leave the remainder untouched.
                parts.append(text[pos:])
                break
            if j == i + len(opener):
                cpos = j + 1
                continue
            end = j + len(close)
            parts.append(text[pos:i])
            parts.append(_placeholder(storage, text[i:end]))
            pos = end
            break
        if j < 0:
            break
    return ''.join(parts)


def _inline_sweep(text, storage):
    """Extract $...$ and \\(...\\) blocks, mirroring the original regex
    alternation. At each position the earliest valid opener is tried; a
    failed opener (no closer before the line end, or a fused '$$' closer)
    causes the engine to retry later positions on the same line, where the
    other delimiter may still win.

    Linear: four monotonic finders ($, \\(, \\), newline) each scan a given
    byte at most once; bounded lookups reuse already-found candidates."""
    n = len(text)

    def make_finder(needle, is_valid):
        state = {'cur': 0, 'idx': -1}

        def next_after(after):
            while True:
                if state['idx'] >= after:
                    return state['idx']
                start = max(after, state['cur'])
                k = text.find(needle, start)
                if k < 0:
                    state['cur'] = n
                    state['idx'] = -1
                    return -1
                state['cur'] = k + len(needle)
                if is_valid(k):
                    state['idx'] = k
                    return k

        return next_after

    # '$' opener: neither preceded nor followed by another '$'.
    def dollar_ok(k):
        return (k == 0 or text[k - 1] != '$') and (
            k + 1 >= n or text[k + 1] != '$')

    find_dollar = make_finder('$', dollar_ok)
    # Raw '$' stream for closer lookups: the body [^$\n] cannot contain
    # '$', so only the first raw '$' after the opener can be the close.
    find_raw_dollar = make_finder('$', lambda k: True)
    find_paren = make_finder('\\(', lambda k: True)
    find_paren_close = make_finder('\\)', lambda k: True)
    find_newline = make_finder('\n', lambda k: True)

    parts = []
    pos = 0
    while pos < n:
        di = find_dollar(pos)
        pi = find_paren(pos)
        if di == -1 and pi == -1:
            break  # final literal chunk appended after the loop
        if pi == -1 or (di != -1 and di < pi):
            i = di
            nl = find_newline(i + 1)
            bound = nl if nl != -1 else n
            # Close at the first '$' strictly after i on the same line
            # whose following character is not '$' (the (?!\\$) check).
            k = find_raw_dollar(i + 1)
            if k != -1 and i < k < bound and k != i + 1 and (
                text[k + 1:k + 2] != '$'
            ):
                parts.append(text[pos:i])
                parts.append(_placeholder(storage, text[i:k + 1]))
                pos = k + 1
                continue
            # Opener fails; its character is now literal and positions on
            # the rest of the line are retried (a '\(' opener may win).
            parts.append(text[pos:i + 1])
            pos = i + 1
        else:
            i = pi
            nl = find_newline(i + 2)
            bound = nl if nl != -1 else n
            # Close at the first '\)' strictly after i on the same line.
            k = find_paren_close(i + 2)
            if k != -1 and i < k < bound:
                parts.append(text[pos:i])
                parts.append(_placeholder(storage, text[i:k + 2]))
                pos = k + 2
                continue
            # Opener fails (no same-line close); opener chars are literal
            # and later same-line positions are retried (a '$' may win).
            parts.append(text[pos:i + 2])
            pos = i + 2

    parts.append(text[pos:])
    return ''.join(parts)


def _protect_math(text):
    storage = []
    # Same ordering as the original regexes: display math ($$ then \[) is
    # extracted first, so its contents can never be consumed as inline math.
    text = _display_sweep(text, '$$', '$$', storage)
    text = _display_sweep(text, '\\[', '\\]', storage)
    text = _inline_sweep(text, storage)
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

    text, math_blocks = _protect_math(text)
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
