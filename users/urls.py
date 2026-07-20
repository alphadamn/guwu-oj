from django.urls import path
from .views import (
    CustomLoginView,
    CustomLogoutView,
    register,
    profile,
    points_center,
    edit_profile,
    avatar,
    send_verification_code,
    password_reset_request,
    password_reset_confirm,
    captcha_image,
    verify_avatar_captcha_view,
    clear_punishment_notice,
)

urlpatterns = [
    path('login/', CustomLoginView.as_view(), name='login'),
    path('logout/', CustomLogoutView.as_view(), name='logout'),
    path('register/', register, name='register'),
    path('send-verification-code/', send_verification_code, name='send_verification_code'),
    path('captcha/image/', captcha_image, name='captcha_image'),
    path('avatar-captcha/verify/', verify_avatar_captcha_view, name='verify_avatar_captcha'),
    path('clear-punishment-notice/', clear_punishment_notice, name='clear_punishment_notice'),
    path('password-reset/', password_reset_request, name='password_reset'),
    path('password-reset/<str:email>/', password_reset_confirm, name='password_reset_confirm'),
    path('profile/<str:username>/', profile, name='profile'),
    path('points/', points_center, name='points_center'),
    path('edit/', edit_profile, name='edit_profile'),
    path('avatar/<str:username>/', avatar, name='avatar'),
]

