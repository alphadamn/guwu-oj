import re

import requests

LUOGU_BASE = 'https://www.luogu.com.cn'
LUOGU_HEADERS = {
    'x-lentille-request': 'content-only',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': f'{LUOGU_BASE}/',
}

LUOGU_DIFFICULTY_MAP = {
    0: '入门',
    1: '入门',
    2: '普及-',
    3: '普及',
    4: '普及+',
    5: '提高-',
    6: '提高',
    7: '提高+',
}


class LuoguFetchError(Exception):
    pass


def normalize_pid(raw):
    raw = (raw or '').strip().upper()
    if not raw:
        raise LuoguFetchError('请输入洛谷题号，例如 P1000')
    if raw.isdigit():
        raw = f'P{raw}'
    if not re.fullmatch(r'P\d+', raw):
        raise LuoguFetchError('题号格式无效，应为 P1000 或 1000')
    return raw


def fix_luogu_markdown_links(text):
    if not text:
        return ''
    text = re.sub(r'\]\(/', f']({LUOGU_BASE}/', text)
    return text


def fetch_luogu_problem(pid):
    pid = normalize_pid(pid)
    url = f'{LUOGU_BASE}/problem/{pid}'
    try:
        resp = requests.get(url, headers=LUOGU_HEADERS, timeout=20)
    except requests.RequestException as exc:
        raise LuoguFetchError(f'请求洛谷失败: {exc}') from exc

    if resp.status_code != 200:
        raise LuoguFetchError(f'洛谷返回 HTTP {resp.status_code}')

    try:
        payload = resp.json()
    except ValueError as exc:
        raise LuoguFetchError('洛谷响应不是 JSON，请稍后重试') from exc

    problem = payload.get('data', {}).get('problem')
    if not problem:
        raise LuoguFetchError('未找到题目数据')

    content = problem.get('content') or problem.get('contenu') or {}
    background = fix_luogu_markdown_links(content.get('background') or '')
    description = fix_luogu_markdown_links(content.get('description') or '')
    format_i = fix_luogu_markdown_links(content.get('formatI') or '')
    format_o = fix_luogu_markdown_links(content.get('formatO') or '')
    hint = fix_luogu_markdown_links(content.get('hint') or '')

    desc_parts = []
    if background:
        desc_parts.append('## 题目背景\n\n' + background)
    if description:
        desc_parts.append('## 题目描述\n\n' + description)
    full_description = '\n\n'.join(desc_parts) or f'（从洛谷 {pid} 导入，暂无描述）'

    samples = problem.get('samples') or content.get('samples') or []
    sample_input = ''
    sample_output = ''
    test_cases = []
    for inp, out in samples:
        inp = inp or ''
        out = out or ''
        test_cases.append((inp, out))
        if not sample_input:
            sample_input = inp
            sample_output = out

    limits = problem.get('limits') or {}
    times = limits.get('time') or [1000]
    memories = limits.get('memory') or [262144]
    time_limit = int(times[0]) if times else 1000
    memory_kb = int(memories[0]) if memories else 262144
    memory_limit = max(memory_kb // 1024, 1)

    difficulty = LUOGU_DIFFICULTY_MAP.get(
        int(problem.get('difficulty', 1)),
        '普及',
    )

    while len(test_cases) < 3:
        if test_cases:
            test_cases.append(test_cases[0])
        else:
            test_cases.append(('1', '1'))

    return {
        'luogu_pid': pid,
        'title': f'[{pid}] {problem.get("name", pid)}',
        'description': full_description,
        'input_format': format_i or '见题目描述',
        'output_format': format_o or '见题目描述',
        'sample_input': sample_input,
        'sample_output': sample_output,
        'hint': hint,
        'difficulty': difficulty,
        'time_limit': time_limit,
        'memory_limit': memory_limit,
        'tags': f'洛谷,{pid}',
        'test_cases': test_cases,
    }
