from decimal import Decimal

from django.db import transaction
from django.db.models import F

from .models import PointLedgerEntry


class InsufficientPoints(Exception):
    pass


def apply_points(*, user_id, amount, event_type, event_key, description=''):
    """Apply an event once and return ``(entry, created)``.

    The user row lock serializes balance mutations. The ledger uniqueness
    constraint makes task retries and duplicated HTTP requests harmless.
    """
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

