from django.contrib import admin
from django.urls import path, include
from django.views.generic import TemplateView

from django.conf.urls import handler404, handler403
from oj_project.views import custom_404_view, custom_403_view

handler404 = custom_404_view
handler403 = custom_403_view

from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('problems.urls')),
    path('users/', include('users.urls')),
    path('submissions/', include('submissions.urls')),
    path('contests/', include('contests.urls')),
    path('handbook/', include('handbook.urls')),
    path('search/', include('search.urls')),
    path('health/', include('health.urls')),
    path('devlog/', include('devlog.urls')),
    path('ai/', include('ai_assistant.urls')),
    path('tickets/', include('tickets.urls')),
    path('internal/judge/', include('submissions.internal_urls')),
    path('', include('django_prometheus.urls')),
    path('privacy-policy/', TemplateView.as_view(template_name='legal/privacy_policy.html'), name='privacy_policy'),
    path('terms-of-service/', TemplateView.as_view(template_name='legal/terms_of_service.html'), name='terms_of_service'),
]
