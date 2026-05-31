from . import algorithms, optimization
from .languages import CATEGORY as LANGUAGES_CATEGORY

HANDBOOK_CATEGORIES = {
    'languages': LANGUAGES_CATEGORY,
    'algorithms': algorithms.CATEGORY,
    'optimization': optimization.CATEGORY,
}


def list_categories():
    return [
        {'slug': slug, **{k: v for k, v in cat.items() if k not in ('articles', 'article_order', 'groups')}}
        for slug, cat in HANDBOOK_CATEGORIES.items()
    ]


def list_categories_with_articles():
    return [get_category(slug) for slug in HANDBOOK_CATEGORIES]


def get_category(slug):
    cat = HANDBOOK_CATEGORIES.get(slug)
    if not cat:
        return None
    order = cat.get('article_order', list(cat['articles'].keys()))
    article_list = []
    for s in order:
        a = cat['articles'][s]
        article_list.append({
            'slug': s,
            'title': a['title'],
            'summary': a.get('summary', ''),
            'group': a.get('group', ''),
            'group_id': a.get('group_id', ''),
        })
    return {
        **{k: v for k, v in cat.items() if k != 'articles'},
        'slug': slug,
        'article_list': article_list,
    }


def get_grouped_article_list(category_slug):
    """Build list with group headers for templates."""
    cat = get_category(category_slug)
    if not cat:
        return []
    items = []
    last_group = None
    for art in cat['article_list']:
        if art.get('group') and art['group'] != last_group:
            items.append({'type': 'header', 'title': art['group']})
            last_group = art['group']
        items.append({'type': 'article', **art})
    return items


def get_article(category_slug, article_slug):
    cat = HANDBOOK_CATEGORIES.get(category_slug)
    if not cat:
        return None
    article = cat['articles'].get(article_slug)
    if not article:
        return None
    return {
        'category_slug': category_slug,
        'category_title': cat['title'],
        'slug': article_slug,
        **article,
    }
