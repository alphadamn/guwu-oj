"""Non-interactive, hidden proof-of-work captcha (ALTCHA v2, PBKDF2/SHA-512).

Thin wrapper around the official ``altcha`` v2 library
(https://pypi.org/project/altcha/), configured for **PBKDF2/SHA-512**.

PBKDF2 is CPU-bound (iterated HMAC-SHA-512) and runs *natively* in the browser
through WebCrypto's ``SubtleCrypto.deriveBits`` (hardware-accelerated, no
WASM), so a solve costs a fraction of a second while still raising the
per-attempt CPU cost against bots. It is *not* memory-hard — for GPU/ASIC
resistance use ``ARGON2ID`` (WASM) instead, at the cost of a slower solve.

Protocol (ALTCHA PoW v2, deterministic-effort mode)
---------------------------------------------------
1. The server picks a random ``counter`` in ``[COUNTER_MIN, COUNTER_MAX]`` and
   pre-computes ``derived_key = PBKDF2-SHA512(password=nonce+counter,
   salt=salt, iterations=KDF_COST)`` once. The challenge carries the first
   ``key_length/2`` bytes of that key as ``keyPrefix`` plus an HMAC
   ``keySignature`` of the full key.
2. The client brute-forces ``counter`` from 0 upward, recomputing PBKDF2 each
   time, until the derived key starts with ``keyPrefix``.
3. The server verifies in O(1) via the ``keySignature`` HMAC (no PBKDF2
   recompute).

The captcha is non-interactive: the solver runs in a background worker and the
user only sees the submit resolve a moment later.
"""
from __future__ import annotations

import base64
import json
import logging
import secrets
import time

from django.conf import settings
from django.core.cache import cache

import altcha as _altcha_v2

logger = logging.getLogger(__name__)

ALGORITHM = 'PBKDF2/SHA-512'
KDF_COST = 50000             # PBKDF2 iterations
KEY_LENGTH = 32              # derived key bytes
COUNTER_MIN = 10             # deterministic counter range -> avg ~30 solves
COUNTER_MAX = 70
DEFAULT_TTL_SECONDS = 600

_CACHE_PREFIX = 'altcha'


def _hmac_secrets() -> tuple[str, str]:
    """Return (params_secret, key_secret) derived from the Django SECRET_KEY."""
    secret = getattr(settings, 'SECRET_KEY', '') or ''
    return (
        f'altcha-params:{secret}',
        f'altcha-key:{secret}',
    )


def generate_challenge(ttl_seconds=None, max_number=None) -> dict:
    """Return an ALTCHA v2 (PBKDF2/SHA-512) challenge dict for the client."""
    ttl = int(ttl_seconds or DEFAULT_TTL_SECONDS)
    params_secret, key_secret = _hmac_secrets()
    counter = secrets.randbelow(COUNTER_MAX - COUNTER_MIN + 1) + COUNTER_MIN
    challenge = _altcha_v2.create_challenge(
        ALGORITHM,
        cost=KDF_COST,
        counter=counter,
        key_length=KEY_LENGTH,
        expires_at=int(time.time()) + ttl,
        hmac_secret=params_secret,
        hmac_key_secret=key_secret,
    )
    return challenge.to_dict()


def _extract_nonce(raw) -> str | None:
    """Extract the challenge nonce from a base64 payload string or dict."""
    try:
        if isinstance(raw, dict):
            return raw.get('challenge', {}).get('parameters', {}).get('nonce')
        data = json.loads(base64.b64decode(raw, validate=True).decode('utf-8'))
        return data.get('challenge', {}).get('parameters', {}).get('nonce')
    except Exception:
        return None


def verify_solution(raw, *, consume: bool = True) -> bool:
    """Verify a solved ALTCHA v2 payload. One-shot (anti-replay) by default."""
    nonce = _extract_nonce(raw)
    if not nonce:
        return False

    if consume:
        used_key = f'{_CACHE_PREFIX}:used:{nonce}'
        try:
            if cache.add(used_key, '1', timeout=DEFAULT_TTL_SECONDS) is False:
                logger.warning('ALTCHA replay attempt (%s...)', nonce[:12])
                return False
        except Exception as exc:  # pragma: no cover - cache outage
            logger.warning('ALTCHA replay marker failed (%s); allowing', exc)

    params_secret, key_secret = _hmac_secrets()
    result = _altcha_v2.verify_solution(
        raw,
        hmac_secret=params_secret,
        hmac_key_secret=key_secret,
    )
    return bool(result.verified)
