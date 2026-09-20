"""Internal worker-facing judge API URLs (Phase 3)."""

from django.urls import path

from . import internal_views

urlpatterns = [
    path('claim/', internal_views.claim_view),
    path('heartbeat/', internal_views.heartbeat_view),
]
