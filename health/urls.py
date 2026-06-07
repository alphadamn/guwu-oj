from django.urls import path
from .views import health_check

urlpatterns = [
    path('', health_check, name='health_check'),
]
