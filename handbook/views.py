from django.shortcuts import render
from django.http import Http404

from problems.markdown_utils import render_markdown
from .content import (
    HANDBOOK_CATEGORIES,
    get_article,
    get_category,
    get_grouped_article_list,
    list_categories,
    list_categories_with_articles,
)


def handbook_index(request):
    categories = list_categories()
    return render(request, 'handbook/index.html', {'categories': categories})


def handbook_category(request, category_slug):
    category = get_category(category_slug)
    if not category:
        raise Http404('分类不存在')
    grouped_items = get_grouped_article_list(category_slug)
    return render(request, 'handbook/category.html', {
        'category': category,
        'grouped_items': grouped_items,
    })


def handbook_article(request, category_slug, article_slug):
    article = get_article(category_slug, article_slug)
    if not article:
        raise Http404('文章不存在')
    article['html_content'] = render_markdown(article['content'])
    category = get_category(category_slug)
    sidebar_groups = []
    if category_slug == 'languages':
        cat_data = HANDBOOK_CATEGORIES['languages']
        for g in cat_data.get('groups', []):
            sidebar_groups.append({
                'title': g['title'],
                'articles': [
                    {
                        'slug': s,
                        'title': cat_data['articles'][s]['title'],
                        'active': s == article_slug,
                    }
                    for s in cat_data['article_order']
                    if cat_data['articles'][s].get('group_id') == g['id']
                ],
            })
    else:
        sidebar_groups = None

    return render(request, 'handbook/article.html', {
        'article': article,
        'category': category,
        'categories': list_categories_with_articles(),
        'sidebar_groups': sidebar_groups,
    })
