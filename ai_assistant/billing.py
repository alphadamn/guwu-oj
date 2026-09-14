"""Stripe billing: price provisioning, Checkout, subscription sync, webhooks.

Products/prices are created once (idempotent, keyed by metadata) and their IDs
cached in the :class:`BillingConfig` singleton. Authoritative plan state is
synced from Stripe webhooks; the post-checkout success page performs the same
sync so the feature also works before a webhook endpoint is configured.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone as dt_timezone

import stripe
from django.conf import settings
from django.urls import reverse

from .constants import (
    INTERVAL_YEAR,
    PAID_PLANS,
    PLAN_PLUS,
    PLAN_PRO,
    PLAN_PRODUCT_NAMES,
    PRICE_FEN,
    STRIPE_CURRENCY,
    STRIPE_INTERVAL,
)
from .models import BillingConfig, Subscription

logger = logging.getLogger(__name__)


class BillingError(Exception):
    """Billing is misconfigured or Stripe rejected the request."""


# Stripe subscription statuses that entitle the user to the paid plan.
_ACTIVE_STATUSES = {'active', 'trialing', 'past_due', 'unpaid'}

# Newer account-default API versions removed Subscription.current_period_start
# / current_period_end (replaced by billing_schedules). Pin the last version
# that still returns them so we can display "有效期至" reliably.
STRIPE_API_VERSION = '2024-06-20'


def _client():
    if not settings.STRIPE_SECRET_KEY:
        raise BillingError('支付尚未配置，请联系管理员。')
    stripe.api_key = settings.STRIPE_SECRET_KEY
    stripe.api_version = STRIPE_API_VERSION
    return stripe


def _ts_to_dt(value):
    """Normalise a Stripe date field (epoch int, or already a datetime) to an
    aware UTC datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt_timezone.utc)
    return datetime.fromtimestamp(value, tz=dt_timezone.utc)


def _meta_value(obj, key, default=None):
    """Read a metadata value from a Stripe object (metadata is a typed
    StripeObject in stripe-python v15, not a plain dict)."""
    metadata = getattr(obj, 'metadata', None)
    if metadata is None:
        return default
    return getattr(metadata, key, default)


# ---------------------------------------------------------------------------
# Product / price provisioning
# ---------------------------------------------------------------------------
def _resource_alive(obj) -> bool:
    return bool(obj) and getattr(obj, 'active', True)


def ensure_prices() -> dict:
    """Return ``{(plan, interval): price_id}``, creating resources as needed."""
    st = _client()
    config = BillingConfig.get_solo()
    dirty = False
    # Track plans whose product was (re)created so we also rebuild prices
    # — an active price tied to an archived product is unusable at Checkout.
    plans_with_new_product: set[str] = set()

    # Products ----------------------------------------------------------------
    for plan in PAID_PLANS:
        product_id = config.product_id(plan)
        product = None
        if product_id:
            try:
                product = st.Product.retrieve(product_id)
            except stripe.InvalidRequestError:
                product = None
        if not _resource_alive(product) or _meta_value(product, 'guwu_plan') != plan:
            product = st.Product.create(
                name=PLAN_PRODUCT_NAMES[plan],
                metadata={'guwu_plan': plan},
            )
            config.set_product_id(plan, product.id)
            dirty = True
            plans_with_new_product.add(plan)

    # Prices ------------------------------------------------------------------
    price_map = {}
    for (plan, interval), amount in PRICE_FEN.items():
        price_id = config.price_id(plan, interval)
        price = None
        if price_id:
            try:
                price = st.Price.retrieve(price_id)
            except stripe.InvalidRequestError:
                price = None
        valid = (
            _resource_alive(price)
            and getattr(price, 'unit_amount', None) == amount
            and _meta_value(price, 'guwu_plan') == plan
            and _meta_value(price, 'guwu_interval') == interval
        )
        # If the product was (re)created, the old price still points to the
        # archived product — Stripe rejects it at Checkout even though the
        # price itself is active. Force a rebuild.
        if plan in plans_with_new_product:
            valid = False
        if not valid:
            # The old price may still be active (e.g. amount changed); archive
            # it so Checkout can only ever see the current price. Existing
            # subscriptions keep their locked-in price until they lapse.
            if price is not None and getattr(price, 'active', False):
                try:
                    st.Price.modify(price.id, active=False)
                except stripe.InvalidRequestError:
                    pass
            meta = {'guwu_plan': plan, 'guwu_interval': interval}
            price = st.Price.create(
                product=config.product_id(plan),
                unit_amount=amount,
                currency=STRIPE_CURRENCY,
                recurring=STRIPE_INTERVAL[interval],
                metadata=meta,
            )
            config.set_price_id(plan, interval, price.id)
            dirty = True
        price_map[(plan, interval)] = price.id

    if dirty:
        config.save(update_fields=[
            'plus_product_id', 'pro_product_id',
            'plus_monthly_price_id', 'plus_yearly_price_id',
            'pro_monthly_price_id', 'pro_yearly_price_id',
            'updated_at',
        ])
    return price_map


# ---------------------------------------------------------------------------
# Customer / Checkout
# ---------------------------------------------------------------------------
def _get_or_create_local(user) -> Subscription:
    sub, _ = Subscription.objects.get_or_create(
        user=user, defaults={'plan': 'free', 'status': Subscription.Status.ACTIVE}
    )
    return sub


def get_or_create_customer(user, sub: Subscription | None = None) -> str:
    st = _client()
    sub = sub or _get_or_create_local(user)
    if sub.stripe_customer_id:
        try:
            st.Customer.retrieve(sub.stripe_customer_id)
            return sub.stripe_customer_id
        except stripe.InvalidRequestError:
            pass
    customer = st.Customer.create(
        email=user.email or None,
        name=user.username,
        metadata={'user_id': str(user.id)},
    )
    sub.stripe_customer_id = customer.id
    sub.save(update_fields=['stripe_customer_id', 'updated_at'])
    return customer.id


def create_checkout_session(request, user, plan: str, interval: str):
    if plan not in PAID_PLANS or interval not in STRIPE_INTERVAL:
        raise BillingError('套餐或计费周期无效。')
    st = _client()
    prices = ensure_prices()
    sub = _get_or_create_local(user)
    if sub.is_paid_active and sub.plan == PLAN_PRO and plan == PLAN_PLUS:
        # Pro is the top tier: block a downgrade purchase (Plus) while Pro is
        # still active. Upgrading Plus -> Pro remains allowed.
        raise BillingError('你已是 Pro 会员，享有 Plus 的全部权益，无需购买 Plus。')
    customer_id = get_or_create_customer(user, sub)

    success_url = request.build_absolute_uri(reverse('ai_assistant:billing_success'))
    success_url = success_url + '?session_id={CHECKOUT_SESSION_ID}'
    cancel_url = request.build_absolute_uri(reverse('ai_assistant:pricing'))

    session = st.checkout.Session.create(
        mode='subscription',
        customer=customer_id,
        line_items=[{'price': prices[(plan, interval)], 'quantity': 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        allow_promotion_codes=True,
        payment_method_collection='if_required',
        client_reference_id=str(user.id),
        metadata={'user_id': str(user.id), 'plan': plan, 'interval': interval},
        subscription_data={
            'metadata': {'user_id': str(user.id), 'plan': plan, 'interval': interval},
        },
    )
    return session


# ---------------------------------------------------------------------------
# Sync Stripe -> local Subscription
# ---------------------------------------------------------------------------
def _apply_subscription(stripe_sub) -> Subscription | None:
    """Upsert the local Subscription row from a Stripe subscription object.

    ``stripe_sub`` must be retrieved with ``expand=['items.data.price']``.
    """
    sid = stripe_sub.id
    items = getattr(stripe_sub, 'items', None)
    data = getattr(items, 'data', None) if items is not None else None
    if not data:
        logger.warning('Stripe subscription %s has no price item', sid)
        return None
    price = getattr(data[0], 'price', None)
    if price is None:
        logger.warning('Stripe subscription %s price not expanded', sid)
        return None

    plan = _meta_value(price, 'guwu_plan')
    if plan not in PAID_PLANS:
        # Fall back to metadata carried on the subscription itself.
        plan = _meta_value(stripe_sub, 'plan')
    if plan not in PAID_PLANS:
        logger.warning('Cannot map Stripe subscription %s to a plan', sid)
        return None

    recurring = getattr(price, 'recurring', None)
    interval_raw = getattr(recurring, 'interval', None) if recurring is not None else None
    interval = INTERVAL_YEAR if interval_raw == 'year' else 'month'

    customer_id = getattr(stripe_sub, 'customer', '') or ''
    user_id = _meta_value(stripe_sub, 'user_id')

    local = Subscription.objects.filter(stripe_subscription_id=sid).first()
    if local is None and customer_id:
        local = Subscription.objects.filter(stripe_customer_id=customer_id).first()
    if local is None and user_id:
        local = Subscription.objects.filter(user_id=user_id).first()
    if local is None:
        logger.warning('No local user for Stripe subscription %s', sid)
        return None

    stripe_status = getattr(stripe_sub, 'status', '') or ''
    active = stripe_status in _ACTIVE_STATUSES
    local.stripe_subscription_id = sid
    if customer_id:
        local.stripe_customer_id = customer_id
    local.interval = interval
    local.current_period_start = _ts_to_dt(getattr(stripe_sub, 'current_period_start', None))
    local.current_period_end = _ts_to_dt(getattr(stripe_sub, 'current_period_end', None))
    local.cancel_at_period_end = bool(getattr(stripe_sub, 'cancel_at_period_end', False))
    if active:
        local.plan = plan
        local.status = Subscription.Status.ACTIVE
    else:
        local.status = Subscription.Status.CANCELED
    local.save()
    return local


def sync_subscription(stripe_subscription_id: str) -> Subscription | None:
    st = _client()
    stripe_sub = st.Subscription.retrieve(
        stripe_subscription_id, expand=['items.data.price']
    )
    return _apply_subscription(stripe_sub)


def sync_checkout_session(session_id: str, *, expected_user=None) -> Subscription | None:
    """Provision from a completed Checkout Session. Returns None if not paid."""
    st = _client()
    session = st.checkout.Session.retrieve(
        session_id,
        expand=['subscription', 'subscription.items.data.price'],
    )
    if getattr(session, 'mode', None) != 'subscription' or not session.subscription:
        return None
    # Only the buyer may provision through the success redirect.
    if expected_user is not None:
        ref = str(getattr(session, 'client_reference_id', '') or '')
        if ref and ref != str(expected_user.id):
            return None
    return _apply_subscription(session.subscription)


def cancel_at_period_end(sub: Subscription) -> None:
    if not sub.stripe_subscription_id:
        raise BillingError('该订阅没有关联的 Stripe 订阅。')
    st = _client()
    st.Subscription.modify(sub.stripe_subscription_id, cancel_at_period_end=True)
    sub.cancel_at_period_end = True
    sub.save(update_fields=['cancel_at_period_end', 'updated_at'])


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------
def parse_webhook_event(raw_body: bytes, sig_header: str):
    """Verify and parse a Stripe webhook. Falls back to unverified JSON only
    when no webhook secret is configured (test setups)."""
    secret = settings.STRIPE_WEBHOOK_SECRET
    if secret:
        return stripe.Webhook.construct_event(raw_body, sig_header, secret)
    logger.warning('STRIPE_WEBHOOK_SECRET not set; accepting unverified webhook')
    return json.loads(raw_body.decode('utf-8'))


def handle_event(event) -> None:
    event_type = event.get('type') if isinstance(event, dict) else event.type
    data_object = event['data']['object'] if isinstance(event, dict) else event.data.object

    if event_type == 'checkout.session.completed':
        sid = data_object.get('subscription') if isinstance(data_object, dict) else data_object.subscription
        if sid:
            sync_subscription(sid)
    elif event_type in ('customer.subscription.updated', 'customer.subscription.created'):
        sid = data_object.get('id') if isinstance(data_object, dict) else data_object.id
        if sid:
            sync_subscription(sid)
    elif event_type in ('customer.subscription.deleted',):
        sid = data_object.get('id') if isinstance(data_object, dict) else data_object.id
        local = Subscription.objects.filter(stripe_subscription_id=sid).first()
        if local:
            local.status = Subscription.Status.CANCELED
            local.cancel_at_period_end = True
            local.save(update_fields=['status', 'cancel_at_period_end', 'updated_at'])
