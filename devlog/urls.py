from django.urls import path

from . import views

app_name = 'devlog'

urlpatterns = [
    path('', views.status_page, name='status'),
    path('entry/add/', views.add_entry, name='add_entry'),
    path('rescan/', views.rescan, name='rescan'),
    path('change/<int:pk>/annotate/', views.annotate_change, name='annotate_change'),
]
