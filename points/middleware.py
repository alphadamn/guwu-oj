"""Expose daily check-in availability for authenticated frontend visits."""

import logging

from django.utils.deprecation import MiddlewareMixin
from django.utils import timezone

from .models import DailyCheckIn, PointConfig

logger = logging.getLogger(__name__)


class DailyCheckInMiddleware(MiddlewareMixin):
    EXCLUDED_PREFIXES = (
        '/admin/', '/static/', '/media/', '/health/', '/metrics', '/devlog/', '/rq/',
        '/users/logout/',
    )

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not self._should_check_in(request):
            return None
        try:
            request.daily_checkin_available = not DailyCheckIn.objects.filter(
                user_id=request.user.id, day=timezone.localdate(),
            ).exists()
            config = PointConfig.get_solo()
            request.daily_checkin_rewards = [
                ('第 1 天', config.daily_checkin_day_1_points),
                ('第 2 天', config.daily_checkin_day_2_points),
                ('第 3 天', config.daily_checkin_day_3_points),
                ('第 4 天', config.daily_checkin_day_4_points),
                ('第 5 天+', config.daily_checkin_day_5_plus_points),
            ]
        except Exception:
            logger.exception('Daily check-in availability lookup failed for user %s', request.user.id)
            request.daily_checkin_available = False
            request.daily_checkin_rewards = []
        return None

    def _should_check_in(self, request):
        path = getattr(request, 'path', '') or ''
        accept = request.META.get('HTTP_ACCEPT', '') or ''
        return (
            request.method in ('GET', 'HEAD')
            and getattr(request.user, 'is_authenticated', False)
            and not any(path.startswith(prefix) for prefix in self.EXCLUDED_PREFIXES)
            and ('text/html' in accept.lower() or accept == '' or accept.startswith('*/*'))
        )
