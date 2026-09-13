"""Plan resolution and calendar-window quota counting.

Quota windows use the site's local timezone (Asia/Shanghai):
- day:   since local 00:00
- week:  since local Monday 00:00
- month: since the 1st, local 00:00

Only *successful* generations count, so an upstream (DeepSeek) failure never
burns a user's quota.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from django.utils import timezone

from .constants import PLAN_FREE, PLAN_LABELS, PLAN_QUOTAS
from .models import AIGeneration, Subscription


def _local_day_start(day, tz):
    return timezone.make_aware(datetime.combine(day, time.min), tz)


def window_starts(now=None) -> dict:
    """Return the aware datetimes when the current day / week / month began."""
    local = timezone.localtime(now or timezone.now())
    tz = local.tzinfo
    today = local.date()
    monday = today - timedelta(days=today.weekday())
    month_first = today.replace(day=1)
    return {
        'day': _local_day_start(today, tz),
        'week': _local_day_start(monday, tz),
        'month': _local_day_start(month_first, tz),
    }


def next_reset(now=None) -> dict:
    """Aware datetimes at which each window counter resets."""
    local = timezone.localtime(now or timezone.now())
    tz = local.tzinfo
    today = local.date()
    tomorrow = today + timedelta(days=1)
    # Next Monday (if today is Monday, the next reset is tomorrow).
    days_until_monday = 7 - today.weekday()
    next_monday = today + timedelta(days=days_until_monday)
    if today.month == 12:
        next_month = today.replace(year=today.year + 1, month=1, day=1)
    else:
        next_month = today.replace(month=today.month + 1, day=1)
    return {
        'day': _local_day_start(tomorrow, tz),
        'week': _local_day_start(next_monday, tz),
        'month': _local_day_start(next_month, tz),
    }


def resolve_plan(user) -> tuple[str, Subscription | None]:
    """Return ``(plan_code, Subscription_or_None)`` for a user."""
    if not getattr(user, 'is_authenticated', False):
        return PLAN_FREE, None
    sub = Subscription.objects.filter(user=user).first()
    if sub is not None and sub.is_paid_active:
        return sub.plan, sub
    return PLAN_FREE, sub


def get_usage(user, now=None) -> dict:
    """Count successful generations in each trailing calendar window."""
    starts = window_starts(now)
    base = AIGeneration.objects.filter(user=user, success=True)
    return {
        'day': base.filter(created_at__gte=starts['day']).count(),
        'week': base.filter(created_at__gte=starts['week']).count(),
        'month': base.filter(created_at__gte=starts['month']).count(),
    }


@dataclass
class QuotaStatus:
    plan: str
    plan_label: str
    limits: dict          # window -> int|None
    used: dict            # window -> int
    remaining: dict       # window -> int|None
    resets: dict          # window -> aware datetime
    allowed: bool
    reason: str = ''      # 'day' | 'week' | 'month' | ''

    def as_dict(self) -> dict:
        return {
            'plan': self.plan,
            'plan_label': self.plan_label,
            'limits': self.limits,
            'used': self.used,
            'remaining': self.remaining,
            'resets': self.resets,
            'allowed': self.allowed,
            'reason': self.reason,
        }


def get_quota_status(user, now=None) -> QuotaStatus:
    plan, _ = resolve_plan(user)
    limits = PLAN_QUOTAS[plan]
    used = get_usage(user, now)
    remaining = {}
    reason = ''
    for window, limit in limits.items():
        if limit is None:
            remaining[window] = None
        else:
            left = max(0, limit - used[window])
            remaining[window] = left
            if left <= 0 and not reason:
                reason = window
    return QuotaStatus(
        plan=plan,
        plan_label=PLAN_LABELS[plan],
        limits=limits,
        used=used,
        remaining=remaining,
        resets=next_reset(now),
        allowed=reason == '',
        reason=reason,
    )
