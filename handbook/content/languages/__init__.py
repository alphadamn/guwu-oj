from ._merge import build_category
from . import cpp, python, java, c_lang

_META = {
    'title': '竞赛语言',
    'icon': 'bi-braces',
    'description': 'C / C++ / Java / Python 分章节：语法、判断循环、IO、标准库与运算。',
}

_LANGUAGE_GROUPS = [
    {'id': 'cpp', 'title': 'C++', 'articles': cpp.ARTICLES},
    {'id': 'python', 'title': 'Python', 'articles': python.ARTICLES},
    {'id': 'java', 'title': 'Java', 'articles': java.ARTICLES},
    {'id': 'c', 'title': 'C 语言', 'articles': c_lang.ARTICLES},
]

CATEGORY = build_category(_META, _LANGUAGE_GROUPS)
