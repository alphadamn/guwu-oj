"""Plan, quota and price definitions for the AI problem-solving feature.

Quotas
------
- free: deepseek-flash limited to 5 generations / day and 15 / week.
- plus: unlimited per day, 50 generations / calendar month.
- pro:  unlimited per day, 100 generations / calendar month.

``None`` means "unlimited" for that window.

Billing
-------
Stripe subscription in CNY (fen). Plus 29.99/month or 299.99/year;
Pro 49.99/month or 499.99/year.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------
PLAN_FREE = 'free'
PLAN_PLUS = 'plus'
PLAN_PRO = 'pro'

PLAN_CHOICES = (
    (PLAN_FREE, '免费版'),
    (PLAN_PLUS, 'Plus 会员'),
    (PLAN_PRO, 'Pro 会员'),
)
PLAN_LABELS = dict(PLAN_CHOICES)

# Billing period used by Stripe recurring prices.
INTERVAL_MONTH = 'month'
INTERVAL_YEAR = 'year'
INTERVAL_CHOICES = (
    (INTERVAL_MONTH, '按月'),
    (INTERVAL_YEAR, '按年'),
)
INTERVAL_LABELS = dict(INTERVAL_CHOICES)

PAID_PLANS = (PLAN_PLUS, PLAN_PRO)

# window key -> max successful generations (None = unlimited)
PLAN_QUOTAS = {
    PLAN_FREE: {'day': 5, 'week': 15, 'month': None},
    PLAN_PLUS: {'day': None, 'week': None, 'month': 50},
    PLAN_PRO: {'day': None, 'week': None, 'month': 100},
}

# A single AI interaction is deliberately short: the first answer plus at
# most one regeneration ("回答满意 / 重新生成").
MAX_GENERATIONS_PER_SESSION = 2

# During one explanation the model may verify its approach against the real
# judge at most this many times (tool: submit_to_judge).
MAX_JUDGE_TOOL_CALLS = 3

# Cap the source size the model is allowed to hand to the judge tool.
JUDGE_TOOL_MAX_CODE_BYTES = 64 * 1024

# How long a single verification submission may stay queued / judging before
# the tool gives up waiting (the submission itself keeps judging afterwards).
JUDGE_TOOL_WAIT_TIMEOUT_SEC = 120
JUDGE_TOOL_POLL_INTERVAL_SEC = 1.5

# ---------------------------------------------------------------------------
# Stripe billing (CNY). Amounts are stored in fen (integer).
# ---------------------------------------------------------------------------
STRIPE_CURRENCY = 'cny'

# (plan, interval) -> price in fen
PRICE_FEN = {
    (PLAN_PLUS, INTERVAL_MONTH): 2999,
    (PLAN_PLUS, INTERVAL_YEAR): 29999,
    (PLAN_PRO, INTERVAL_MONTH): 4999,
    (PLAN_PRO, INTERVAL_YEAR): 49999,
}

# Display prices in yuan.
PRICE_YUAN = {key: value / 100 for key, value in PRICE_FEN.items()}

# Stripe recurring interval spec for each billing period.
STRIPE_INTERVAL = {
    INTERVAL_MONTH: {'interval': 'month', 'interval_count': 1},
    INTERVAL_YEAR: {'interval': 'year', 'interval_count': 1},
}

PLAN_PRODUCT_NAMES = {
    PLAN_PLUS: '谷物 OJ · Plus 会员',
    PLAN_PRO: '谷物 OJ · Pro 会员',
}

PLAN_DESCRIPTIONS = {
    PLAN_PLUS: '提交优先评测（Plus队列）',
    PLAN_PRO: '提交优先评测+（Pro队列）',
}

# Per-user anti-abuse limit for the generate endpoint (successful or not),
# enforced with the project's sliding-window limiter. Generations / minute.
GENERATE_RATE_LIMIT_PER_MINUTE = 12
