from django.shortcuts import render
from django.views.decorators.cache import cache_page
from django.views.decorators.vary import vary_on_headers

@cache_page(60 * 60)  # 缓存 15 分钟
@vary_on_headers('Host', 'Accept-Language')  # 可选：根据请求头区分缓存
def custom_404_view(request, exception):
    return render(request, '404.html', status=404)