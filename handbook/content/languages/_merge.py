def build_category(meta, language_groups):
    """Merge per-language article dicts into one category with group metadata."""
    articles = {}
    article_order = []
    groups = []

    for group in language_groups:
        group_id = group['id']
        group_title = group['title']
        group_articles = group['articles']
        groups.append({
            'id': group_id,
            'title': group_title,
            'count': len(group_articles),
        })
        for slug, article in group_articles.items():
            articles[slug] = {
                **article,
                'group': group_title,
                'group_id': group_id,
            }
            article_order.append(slug)

    return {
        **meta,
        'articles': articles,
        'article_order': article_order,
        'groups': groups,
    }
