from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from .models import DailyCheckIn, PointConfig, PointLedgerEntry


class InsufficientPoints(Exception):
    pass


def apply_points(*, user_id, amount, event_type, event_key, description=''):
    """Apply an event once and return ``(entry, created)``."""
    from users.models import User

    amount = Decimal(str(amount)).quantize(Decimal('0.0001'))
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        try:
            existing = PointLedgerEntry.objects.get(
                user_id=user_id, event_type=event_type, event_key=event_key,
            )
            return existing, False
        except PointLedgerEntry.DoesNotExist:
            pass

        if amount < 0 and user.points_balance < -amount:
            raise InsufficientPoints

        user.points_balance = F('points_balance') + amount
        user.save(update_fields=['points_balance'])
        user.refresh_from_db(fields=['points_balance'])
        entry = PointLedgerEntry.objects.create(
            user=user,
            amount=amount,
            balance_after=user.points_balance,
            event_type=event_type,
            event_key=event_key,
            description=description,
        )
        return entry, True


def check_in_user(*, user_id, day=None):
    """Atomically record one local-day check-in and award its streak reward."""
    from users.models import User

    day = day or timezone.localdate()
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        existing = DailyCheckIn.objects.filter(user_id=user_id, day=day).first()
        if existing:
            return existing, False

        previous = DailyCheckIn.objects.filter(user_id=user_id).order_by('-day').first()
        streak = previous.streak + 1 if previous and previous.day == day - timedelta(days=1) else 1
        reward = PointConfig.get_solo().reward_for_streak(streak)
        try:
            with transaction.atomic():
                checkin = DailyCheckIn.objects.create(
                    user=user, day=day, streak=streak,
                    points_awarded=Decimal(str(reward)).quantize(Decimal('0.0001')),
                )
        except IntegrityError:
            checkin = DailyCheckIn.objects.get(user_id=user_id, day=day)
            return checkin, False

        apply_points(
            user_id=user_id,
            amount=reward,
            event_type='daily_check_in',
            event_key=day.isoformat(),
            description=f'连续签到第 {streak} 天',
        )
        return checkin, True


def check_in_notice(checkin):
    return {
        'streak': checkin.streak,
        'points': str(checkin.points_awarded),
        'day': checkin.day.isoformat(),
    }
