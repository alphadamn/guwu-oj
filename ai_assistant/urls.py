from django.urls import path

from . import views

app_name = 'ai_assistant'

urlpatterns = [
    path('problem/<int:problem_id>/', views.ask, name='ask'),
    path('problem/<int:problem_id>/generate/', views.generate, name='generate'),
    path('session/<int:session_id>/satisfied/', views.satisfied, name='satisfied'),
    path('pricing/', views.pricing, name='pricing'),
    path('checkout/', views.checkout, name='checkout'),
    path('success/', views.billing_success, name='billing_success'),
    path('cancel/', views.cancel_subscription, name='cancel_subscription'),
    path('webhook/stripe/', views.stripe_webhook, name='stripe_webhook'),
]
