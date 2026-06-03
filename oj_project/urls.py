from django.contrib import admin
from django.urls import path, include
from django.views.generic import TemplateView

from django.conf.urls import handler404
from oj_project.views import custom_404_view

handler404 = custom_404_view

from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('problems.urls')),
    path('users/', include('users.urls')),
    path('submissions/', include('submissions.urls')),
    path('handbook/', include('handbook.urls')),
]
