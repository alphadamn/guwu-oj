"""Chinese algorithm-tag labels for display, search, and AI completion."""
from __future__ import annotations

import re

_CJK_RE = re.compile(r'[\u4e00-\u9fff]')

# Codeforces / common English tags → 中文 OI 标签。
EN_TO_ZH = {
    'math': '数学',
    'greedy': '贪心',
    'implementation': '模拟',
    'dp': '动态规划',
    'data structures': '数据结构',
    'data': '数据结构',
    'structures': '数据结构',
    'brute force': '暴力',
    'brute': '暴力',
    'force': '暴力',
    'constructive algorithms': '构造',
    'constructive': '构造',
    'algorithms': '构造',
    'graphs': '图论',
    'graph': '图论',
    'binary search': '二分',
    'binary': '二分',
    'search': '搜索',
    'dfs and similar': '搜索',
    'dfs': '深度优先搜索',
    'similar': '搜索',
    'trees': '树',
    'strings': '字符串',
    'string': '字符串',
    'number theory': '数论',
    'number': '数论',
    'theory': '数论',
    'combinatorics': '组合数学',
    'sortings': '排序',
    'sorting': '排序',
    'two pointers': '双指针',
    'two': '双指针',
    'pointers': '双指针',
    'bitmasks': '位运算',
    'dsu': '并查集',
    'divide and conquer': '分治',
    'divide': '分治',
    'conquer': '分治',
    'geometry': '计算几何',
    'shortest paths': '最短路',
    'shortest': '最短路',
    'paths': '最短路',
    'games': '博弈',
    'hashing': '哈希',
    'matrices': '矩阵',
    'flows': '网络流',
    'probabilities': '概率期望',
    'fft': '快速傅里叶变换',
    'string suffix structures': '后缀结构',
    'suffix': '后缀结构',
    'graph matchings': '二分图匹配',
    'matchings': '二分图匹配',
    'meet-in-the-middle': '折半搜索',
    'ternary search': '三分',
    'ternary': '三分',
    'expression parsing': '表达式解析',
    'expression': '表达式解析',
    'parsing': '表达式解析',
    '2-sat': '2-SAT',
    '*special': '特殊题',
}

# 给 AI 选用的中文词表（含题库里已出现过的中文标签时会再合并）。
CANONICAL_ZH_TAGS = [
    '模拟', '枚举', '暴力', '贪心', '构造', '排序',
    '数学', '数论', '组合数学', '概率期望', '博弈', '高精度', '矩阵',
    '动态规划', '背包', '区间DP', '树形DP', '状压DP', '数位DP', '记忆化搜索',
    '搜索', '深度优先搜索', '广度优先搜索', '折半搜索',
    '二分', '三分', '双指针', '前缀和', '差分', '分治', '倍增',
    '数据结构', '栈', '队列', '单调栈', '单调队列', '堆', '哈希',
    '并查集', '树状数组', '线段树', '平衡树', 'ST表', '可持久化',
    '字符串', 'KMP', '字典树', '后缀结构', '哈希表',
    '图论', '树', '最短路', '最小生成树', '拓扑排序', '强连通分量',
    '二分图', '二分图匹配', '网络流', '最近公共祖先', '树链剖分',
    '位运算', '线性代数', '快速傅里叶变换', '计算几何', '容斥',
    '差分约束', '2-SAT', '点分治', '启发式合并', '莫队', '扫描线',
    '表达式解析', '特殊题',
]

# 题库标签选择器（类洛谷）使用的分组，顺序即展示顺序。
# 覆盖 CANONICAL_ZH_TAGS 全部标签；题库里额外出现的高频中文标签（如“递推”）
# 也登记在此。
TAG_GROUPS = {
    '基础算法': [
        '模拟', '枚举', '暴力', '贪心', '构造', '排序', '递推',
        '二分', '三分', '双指针', '前缀和', '差分', '分治', '倍增', '容斥',
    ],
    '动态规划': [
        '动态规划', '背包', '区间DP', '树形DP', '状压DP', '数位DP', '记忆化搜索',
    ],
    '搜索': [
        '搜索', '深度优先搜索', '广度优先搜索', '折半搜索',
    ],
    '数学': [
        '数学', '数论', '组合数学', '概率期望', '博弈', '高精度', '矩阵',
        '线性代数', '快速傅里叶变换', '计算几何',
    ],
    '数据结构': [
        '数据结构', '栈', '队列', '单调栈', '单调队列', '堆', '哈希', '哈希表',
        '并查集', '树状数组', '线段树', '平衡树', 'ST表', '可持久化',
        '启发式合并', '莫队', '扫描线',
    ],
    '字符串': [
        '字符串', 'KMP', '字典树', '后缀结构', '表达式解析',
    ],
    '图论': [
        '图论', '树', '最短路', '最小生成树', '拓扑排序', '强连通分量',
        '最近公共祖先', '树链剖分', '二分图', '二分图匹配', '网络流',
        '差分约束', '2-SAT', '点分治',
    ],
    '其他': [
        '位运算', '特殊题',
    ],
}

# 题源标签（原始存储形式），单独成组展示。
SOURCE_TAGS = [
    '洛谷', 'Codeforces', 'AtCoder', 'USACO', 'CodeChef', 'HackerEarth',
    'AOJ', 'Kattis', 'GeeksforGeeks', 'HackerRank',
]

# 所有已分组的算法标签（扁平集合）。
TAG_GROUPS_ALL = {tag for tags in TAG_GROUPS.values() for tag in tags}

DEFAULT_SYSTEM_PROMPT = (
    '你是竞赛编程（OI / ACM）题目标签助手，面向中文题库。\n'
    '根据题面，从给定的中文标签词表中选出 1 到 6 个最贴切的算法或知识点标签。\n'
    '硬性要求：\n'
    '1. 标签必须全部是中文（或词表里已有的中文写法），禁止输出英文标签'
    '（例如 dp、greedy、implementation、graph）。\n'
    '2. 只能使用词表中的原样字符串，禁止自造标签、禁止翻译成词表以外的词。\n'
    '3. 不要输出来源站名、题号、难度或评分。\n'
    '4. 题面信息不足时，仍要给出最接近的词表标签，不要返回空数组。\n'
    '5. 只输出 json 对象，格式：{"tags": ["标签1", "标签2"]}。'
)

DEFAULT_USER_PROMPT = (
    '可选中文标签词表：\n{vocab}\n\n'
    '题号：P{id}\n'
    '标题：{title}\n'
    '难度：{difficulty}\n'
    '题面：\n{statement}'
)

PROMPT_CACHE_KEY = 'problems:tag_complete_prompts'
PROMPT_MAX_LEN = 8000

PROVENANCE_EXACT = {
    '洛谷', 'Luogu', 'Codeforces', 'AtCoder', 'CodeChef', 'SPOJ', 'HDU',
    'POJ', 'UVa', 'UVA', 'Kattis', 'Timus', 'Yandex', 'TopCoder', 'Google',
    'USACO', 'HackerEarth', 'AOJ', 'LibreOJ', '洛谷网', 'GeeksforGeeks', 
    'HackerRank', 'GOLD', 'SILVER', 'BRONZE', 'PLATINUM', 
}

_PROV_PREFIXES = ('cc:', 'cf:', 'contest:', 'usaco:', 'taco:')
_PID_RE = re.compile(r'^P\d+$')
_CF_RATING_RE = re.compile(r'^CF\d+$', re.IGNORECASE)


def is_provenance_tag(tag: str) -> bool:
    text = (tag or '').strip()
    if not text:
        return True
    lower = text.lower()
    if lower.startswith(_PROV_PREFIXES) or text in PROVENANCE_EXACT:
        return True
    if _PID_RE.fullmatch(text) or _CF_RATING_RE.fullmatch(text):
        return True
    return False


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ''))


def to_zh_algorithm_tag(tag: str) -> str | None:
    """Map an algorithm tag to Chinese. Return None if it should not be shown."""
    name = (tag or '').strip()
    if not name:
        return None
    if has_cjk(name):
        return name
    if name in CANONICAL_ZH_TAGS:
        return name
    mapped = EN_TO_ZH.get(name) or EN_TO_ZH.get(name.lower())
    return mapped


def search_aliases(term: str) -> list[str]:
    """English/Chinese equivalents so tag filters match stored CF tags."""
    term = (term or '').strip()
    if not term:
        return []
    aliases = [term]
    mapped = EN_TO_ZH.get(term) or EN_TO_ZH.get(term.lower())
    if mapped and mapped not in aliases:
        aliases.append(mapped)
    for en, zh in EN_TO_ZH.items():
        if zh == term and en not in aliases:
            aliases.append(en)
    return aliases


# --------------------------------------------------------------------------- #
# Exact-match tag selection (Luogu-style picker)
# --------------------------------------------------------------------------- #

# Unlike fuzzy search, tag selection matches whole stored tokens, so an
# English fragment such as "search" must not match the phrase
# "binary search" (which belongs to 二分, not 搜索). A single-word English
# key is usable as an exact alias only when every multi-word phrase
# containing that word maps to the same Chinese tag; multi-word phrases
# themselves are always safe (they are matched as one complete token).
def _build_exact_en_aliases() -> dict[str, list[str]]:
    phrase_owners: dict[str, set[str]] = {}
    for en, zh in EN_TO_ZH.items():
        if ' ' not in en:
            continue
        for word in en.split():
            phrase_owners.setdefault(word.lower(), set()).add(zh)
    result: dict[str, list[str]] = {}
    for en, zh in EN_TO_ZH.items():
        if ' ' in en or phrase_owners.get(en.lower(), set()) <= {zh}:
            result.setdefault(zh, []).append(en)
    return result


EXACT_EN_ALIASES = _build_exact_en_aliases()


def canonical_tag_name(term: str) -> str | None:
    """Map a raw user-picked term to a canonical Chinese tag (or None)."""
    term = (term or '').strip()
    if not term:
        return None
    if term in TAG_GROUPS_ALL or term in SOURCE_TAGS:
        return term
    mapped = EN_TO_ZH.get(term) or EN_TO_ZH.get(term.lower())
    return mapped


def filter_aliases(term: str) -> list[str]:
    """Whole-token aliases for exact tag filtering.

    Returns the tag itself plus safe English equivalents actually stored in
    imported rows (e.g. 动态规划 -> ["动态规划", "dp"]); ambiguous fragments
    like "search" are excluded.
    """
    term = (term or '').strip()
    if not term:
        return []
    aliases = [term]
    for en in EXACT_EN_ALIASES.get(term, []):
        if en not in aliases:
            aliases.append(en)
    return aliases
