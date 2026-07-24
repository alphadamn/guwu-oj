from django.urls import path

from . import views

app_name = 'devlog'

urlpatterns = [
    path('', views.status_page, name='status'),
    path('entry/add/', views.add_entry, name='add_entry'),
    path('rescan/', views.rescan, name='rescan'),
    path('change/<int:pk>/annotate/', views.annotate_change, name='annotate_change'),
    path('refresh-health/', views.refresh_health_checks, name='refresh_health'),
    path('clear-cache/', views.clear_cache, name='clear_cache'),
    path('clear-page-cache/', views.clear_page_cache_view, name='clear_page_cache'),
    path('clear-all-caches/', views.clear_all_caches_view, name='clear_all_caches'),
    path('record-browser-location/', views.record_browser_location, name='record_browser_location'),
]

# Endpoints above remain local and CSRF-protected; no third-party location API.
