from django.urls import path
from .views import CustomLoginView, CustomLogoutView, register, profile, edit_profile

urlpatterns = [
    path('login/', CustomLoginView.as_view(), name='login'),
    path('logout/', CustomLogoutView.as_view(), name='logout'),
    path('register/', register, name='register'),
    path('profile/<str:username>/', profile, name='profile'),
    path('edit/', edit_profile, name='edit_profile'),
]
