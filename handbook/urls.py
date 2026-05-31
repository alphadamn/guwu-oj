from django.urls import path
from . import views

urlpatterns = [
    path('', views.handbook_index, name='handbook_index'),
    path('<slug:category_slug>/', views.handbook_category, name='handbook_category'),
    path('<slug:category_slug>/<slug:article_slug>/', views.handbook_article, name='handbook_article'),
]
