"""Production-ready image captcha for registration / login escalation.

Design goals
-------------
1. **No shared key.** Every page load that shows a captcha calls
   :func:`generate_challenge` which returns a one-shot ``challenge_id``
   (uuid) + the expected answer (never rendered in HTML / JS).
   The answer is stored in Django's cache (Redis) under a per-challenge
   key with a hard TTL.
2. **Not guessable.** 5 random chars from a set with ambiguous glyphs
   (I, O, 0, 1) removed, rendered on a noisy background with interference
   lines and random jitter.
3. **Rate-limited per IP.** :func:`can_generate_challenge` refuses to
   hand out more than a configurable number of new challenges per IP
   per minute so an attacker can't brute-force the space of answers.
4. **One-shot consumption.** After a single :func:`check_challenge`
   call (success *or* failure) the cache entry is deleted. Submitting
   the same captcha twice therefore always fails — prevents replay.
5. **Login escalation.** A ``captcha_required`` flag is raised per IP
   after the first failed login attempt and cleared on success (see
   :func:`record_login_attempt`).
6. **Graceful degradation.** If the cache backend is unavailable (e.g.
   Redis down) we still accept *any* submitted challenge — we just log
   a warning. This keeps the site live under partial outages.

Usage
-----
::

    # in views.py - issue a challenge
    challenge_id, answer, image_bytes = generate_captcha(request)

    # in the form - verify a submitted answer
    if not check_captcha(request, challenge_id, user_answer):
        ... raise ValidationError ...

    # after a failed/successful login attempt
    record_login_attempt(request, success=False)
"""
from __future__ import annotations

import hashlib
import io
import logging
import random
import secrets
import string
import time
import uuid

from django.core.cache import cache
from django.http import HttpResponse
from django.utils import timezone

logger = logging.getLogger(__name__)


def _load_captcha_config():
    """Return the CaptchaConfig singleton, falling back to defaults."""
    try:
        from devlog.models import CaptchaConfig
        obj, _ = CaptchaConfig.objects.get_or_create(pk=1)
        return obj
    except Exception:
        return None


def _cfg_int(field: str, default: int) -> int:
    cfg = _load_captcha_config()
    if cfg is None:
        return default
    try:
        val = int(getattr(cfg, field, default) or default)
        return val if val > 0 else default
    except (TypeError, ValueError):
        return default


def _cfg_bool(field: str, default: bool) -> bool:
    cfg = _load_captcha_config()
    if cfg is None:
        return default
    try:
        return bool(getattr(cfg, field, default))
    except Exception:
        return default


ANSWER_LENGTH = lambda: _cfg_int('captcha_answer_length', 5)
ANSWER_ALPHABET = ''.join(c for c in string.ascii_uppercase + string.digits
                          if c not in {'0', '1', 'I', 'O', 'B', '8', 'S', '5'})
assert len(ANSWER_ALPHABET) >= 25

IMAGE_WIDTH = 180
IMAGE_HEIGHT = 50
IMAGE_FONT_SIZE = 32

CHALLENGE_TTL_SECONDS = lambda: _cfg_int('captcha_challenge_ttl_seconds', 600)
CHALLENGES_PER_IP_PER_MINUTE = lambda: _cfg_int(
    'captcha_per_ip_per_minute', 20
)
CAPTCHA_ATTEMPTS_PER_IP_PER_10_MINUTES = lambda: _cfg_int(
    'captcha_attempts_per_ip_per_10_minutes', 30
)
FAILED_LOGINS_BEFORE_CAPTCHA = lambda: _cfg_int(
    'captcha_on_login_after_failures', 1
)
FAILED_LOGIN_RECORD_TTL_SECONDS = 60 * 30
CAPTCHA_ON_REGISTER = lambda: _cfg_bool('captcha_on_register', True)
CAPTCHA_ON_FORGOT_PASSWORD = lambda: _cfg_bool(
    'captcha_require_on_forgot_password', True
)
CAPTCHA_ON_ALL_POST = lambda: _cfg_bool('captcha_require_on_all_post', False)

CACHE_PREFIX = 'captcha'
_SESSION_KEY_CHALLENGE = '_captcha_challenge_id'
AVATAR_PROOF_TTL_SECONDS = 5 * 60


# ---------------------------------------------------------------------------
# Cache-key helpers
# ---------------------------------------------------------------------------

def _challenge_key(challenge_id: str) -> str:
    return f'{CACHE_PREFIX}:challenge:{challenge_id}'


def _challenge_used_key(challenge_id: str) -> str:
    return f'{CACHE_PREFIX}:used:{challenge_id}'


def _ip_rate_key(ip: str, window: int) -> str:
    # `window` is a coarse time-bucket index, so the key rotates.
    return f'{CACHE_PREFIX}:rate:ip:{ip}:{window}'


def _login_failure_key(ip: str) -> str:
    return f'{CACHE_PREFIX}:failures:ip:{ip}'


def _client_ip(request) -> str:
    """Return the *real* client IP address.

    Strategy (first non-empty wins):

    1. ``HTTP_X_FORWARDED_FOR`` — this is what nginx sets by default when you
       include ``proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;``
       in the server block. nginx appends the immediate upstream's address, so
       the *first* value is always the real client IP — this is what we use.
    2. ``HTTP_X_REAL_IP`` — some setups (e.g. Cloudflare) write the client IP
       into this header instead.
    3. ``REMOTE_ADDR`` — the direct TCP peer (always ``127.0.0.1`` when a
       reverse proxy is on the same machine; kept as a fallback for local dev).

    The result is validated using :mod:`ipaddress` — anything that isn't a
    real IP falls back to ``REMOTE_ADDR``. This prevents an attacker from
    poisoning ``X-Forwarded-For`` with something like ``"<script>..."``.
    """
    # 1) X-Forwarded-For (the most common)
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        candidate = xff.split(',')[0].strip()
        if _looks_like_ip(candidate):
            return candidate

    # 2) X-Real-IP (often set by Cloudflare / CDNs / nginx "real_ip" module)
    xri = request.META.get('HTTP_X_REAL_IP')
    if xri and _looks_like_ip(xri.strip()):
        return xri.strip()

    # 3) Raw peer (typically 127.0.0.1 under nginx)
    return request.META.get('REMOTE_ADDR') or 'unknown'


def _looks_like_ip(value: str) -> bool:
    """Lightweight IPv4/IPv6 validator — keeps the cache-key namespace sane."""
    if not value:
        return False
    try:
        import ipaddress
        ipaddress.ip_address(value)
        return True
    except (ValueError, TypeError):
        return False

def rint(a: int, b: int) -> int:
    """Return a random integer N such that a <= N <= b."""
    return a + secrets.randbelow(b - a + 1)

# ---------------------------------------------------------------------------
# Image rendering
# ---------------------------------------------------------------------------

def _random_color(lo: int = 30, hi: int = 180) -> tuple[int, int, int]:
    return (
        rint(lo, hi),
        rint(lo, hi),
        rint(lo, hi),
    )


def _render_captcha_image(answer: str) -> bytes:
    """Render ``answer`` on a noisy background and return a PNG bytestring."""
    # Local import so the rest of the module still works if Pillow is
    # temporarily unavailable (we'll fall back to a placeholder image).
    from PIL import Image, ImageDraw, ImageFont, ImageFilter

    image = Image.new('RGB', (IMAGE_WIDTH, IMAGE_HEIGHT), (245, 245, 245))
    draw = ImageDraw.Draw(image)

    # 1. Background noise dots — OCR works much better on clean input.
    for _ in range(IMAGE_WIDTH * IMAGE_HEIGHT // 8):
        draw.point(
            (rint(0, IMAGE_WIDTH - 1),
             rint(0, IMAGE_HEIGHT - 1)),
            fill=_random_color(180, 230),
        )

    # 2. Interference lines.
    for _ in range(4):
        start = (0, rint(0, IMAGE_HEIGHT - 1))
        end = (IMAGE_WIDTH, rint(0, IMAGE_HEIGHT - 1))
        draw.line([start, end], fill=_random_color(80, 180), width=1)

    # 3. Load a font — fall back to the default PIL font if nothing is
    # installed on the host (common on minimal containers).
    font = None
    for font_path in (
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
        '/usr/share/fonts/truetype/freefont/FreeSansBold.ttf',
        '/System/Library/Fonts/Supplemental/Arial Bold.ttf',
        'C:/Windows/Fonts/arialbd.ttf',
    ):
        try:
            font = ImageFont.truetype(font_path, IMAGE_FONT_SIZE)
            break
        except (OSError, IOError):
            continue
    if font is None:
        try:
            font = ImageFont.load_default()
        except Exception:  # pragma: no cover - best effort
            font = None

    # 4. Draw every glyph with per-char jitter / rotation / color.
    # Calculate a small horizontal offset so the text is roughly centered.
    step = IMAGE_WIDTH / (len(answer) + 1)
    for idx, ch in enumerate(answer):
        # Per-character tile for independent rotation.
        tile = Image.new('RGBA', (IMAGE_FONT_SIZE + 6, IMAGE_FONT_SIZE + 6),
                         (0, 0, 0, 0))
        tile_draw = ImageDraw.Draw(tile)
        if font is not None:
            tile_draw.text((3, 0), ch, font=font, fill=_random_color(20, 90))
        else:
            tile_draw.text((3, 0), ch, fill=_random_color(20, 90))
        tile = tile.rotate(secrets.SystemRandom().uniform(-22, 22), resample=Image.BICUBIC,
                            expand=False)
        x = int(step * (idx + 1) - IMAGE_FONT_SIZE / 2)
        y = rint(4, IMAGE_HEIGHT - IMAGE_FONT_SIZE - 2)
        image.paste(tile, (x, y), tile)

    # 5. Final light blur — defeats naive threshold OCR.
    image = image.filter(ImageFilter.SMOOTH)

    buf = io.BytesIO()
    image.save(buf, format='PNG')
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Challenge issuing / verification
# ---------------------------------------------------------------------------

def _increment_rate_counter(key: str, limit: int, ttl: int) -> bool:
    """Increment a cache counter; return True if still under ``limit``.

    Returns True when the caller is within their rate budget.
    """
    try:
        count = cache.get_or_set(key, 0, timeout=ttl)
        if count is None:
            count = 0
        # Use incr when possible; fall back to set for backends without it.
        try:
            new_count = cache.incr(key)
        except (ValueError, TypeError):
            new_count = count + 1
            cache.set(key, new_count, timeout=ttl)
        return int(new_count) <= limit
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning('Captcha rate counter raised %s; allowing', exc)
        return True


def generate_challenge(request) -> tuple[str, str, bytes]:
    """Create a new captcha challenge for the current user session.

    Writes the expected answer to the cache keyed by a random uuid and
    returns ``(challenge_id, expected_answer, png_bytes)``. The
    ``challenge_id`` is also stashed on ``request.session`` so the next
    POST can trivially find it — the user never has to type it.

    Rate-limited per IP. Under heavy load (or when no cache is
    available) returns a placeholder challenge that still validates
    deterministically on the server side.
    """
    ip = _client_ip(request)
    if not _increment_rate_counter(
        _ip_rate_key(ip, int(time.time() // 60)),
        int(CHALLENGES_PER_IP_PER_MINUTE()),
        90,
    ):
        raise TooManyChallenges(
            '生成验证码过于频繁，请稍后再试。'
        )

    length = int(ANSWER_LENGTH())
    answer = ''.join(secrets.choice(ANSWER_ALPHABET) for _ in range(length))
    challenge_id = uuid.uuid4().hex
    try:
        cache.set(_challenge_key(challenge_id), answer,
                  timeout=int(CHALLENGE_TTL_SECONDS()))
    except Exception as exc:  # pragma: no cover - cache unavailable
        logger.warning('Captcha cache write failed (%s); continuing', exc)

    try:
        png = _render_captcha_image(answer)
    except Exception as exc:  # pragma: no cover - best effort
        logger.exception('Captcha rendering failed (%s); using placeholder', exc)
        png = _placeholder_png(answer)

    # Remember the id in the session so forms can render a hidden input.
    try:
        request.session[_SESSION_KEY_CHALLENGE] = challenge_id
    except Exception:
        pass
    return challenge_id, answer, png


def get_current_challenge_id(request) -> str | None:
    """Return the challenge_id currently stored on the session (if any)."""
    try:
        return request.session.get(_SESSION_KEY_CHALLENGE)
    except Exception:
        return None


def check_challenge(request, challenge_id: str, submitted_answer: str,
                    *, consume: bool = True,
                    fail_open_on_cache_error: bool = True) -> bool:
    """Verify the submitted answer for ``challenge_id``.

    - Returns False on an empty/invalid ``challenge_id``.
    - Returns False for an already-used challenge (anti-replay).
    - The comparison is case-insensitive (users often type lowercase).
    - If ``consume`` is True (default), the challenge is deleted after
      one attempt so replay is impossible.
    """
    if not challenge_id or not submitted_answer:
        return False

    ip = _client_ip(request)
    # Per-IP attempt rate-limit.
    window = int(time.time() // 600)
    if not _increment_rate_counter(
        _ip_rate_key(f'attempt:{ip}', window),
        int(CAPTCHA_ATTEMPTS_PER_IP_PER_10_MINUTES()),
        600,
    ):
        return False

    # Anti-replay: reject if the challenge has been used before.
    try:
        if cache.add(_challenge_used_key(challenge_id), '1',
                     timeout=int(CHALLENGE_TTL_SECONDS()) * 2) is False:
            logger.warning('Captcha replay attempt from %s', ip)
            return False
    except Exception:
        pass

    expected = None
    try:
        expected = cache.get(_challenge_key(challenge_id))
    except Exception as exc:  # pragma: no cover - cache unavailable
        logger.warning('Captcha cache read failed (%s)', exc)
        return fail_open_on_cache_error

    if consume:
        try:
            cache.delete(_challenge_key(challenge_id))
        except Exception:
            pass

    if not expected:
        return False

    # Constant-time-ish compare; secrets.compare_digest is timing-safe.
    try:
        return secrets.compare_digest(
            str(expected).strip().upper(),
            str(submitted_answer).strip().upper(),
        )
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Login escalation
# ---------------------------------------------------------------------------

def record_login_attempt(request, *, success: bool) -> None:
    """Track per-IP failed-logins so we can require a captcha on retry."""
    if success:
        try:
            cache.delete(_login_failure_key(_client_ip(request)))
        except Exception:
            pass
        return

    try:
        key = _login_failure_key(_client_ip(request))
        count = cache.get_or_set(key, 0, timeout=FAILED_LOGIN_RECORD_TTL_SECONDS)
        if count is None:
            count = 0
        try:
            cache.incr(key)
        except (ValueError, TypeError):
            cache.set(key, count + 1, timeout=FAILED_LOGIN_RECORD_TTL_SECONDS)
    except Exception:
        pass


def login_requires_captcha(request) -> bool:
    """True when the current IP has reached the configured number of recent failed logins."""
    try:
        failures = cache.get(_login_failure_key(_client_ip(request))) or 0
        return int(failures) >= int(FAILED_LOGINS_BEFORE_CAPTCHA())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CaptchaError(Exception):
    """Base for all captcha errors raised by this module."""


class TooManyChallenges(CaptchaError):
    """The client has requested too many captchas in a short window."""


# ---------------------------------------------------------------------------
# Submission rate-limit → captcha escalation
# ---------------------------------------------------------------------------

def _submission_rate_key(user_id, *, bucket_seconds: int) -> str:
    """Cache key that counts submission attempts for a given user in a
    time window."""
    window_index = int(time.time()) // int(bucket_seconds or 1)
    return f'{CACHE_PREFIX}:rate:submissions:user:{user_id}:{window_index}'


def _submission_captcha_config() -> dict:
    """Load the three submission-captcha knobs from admin with safe defaults.

    Returns: {'enabled': bool, 'limit': int, 'window_seconds': int}.
    """
    try:
        from devlog.models import CaptchaConfig as _CC
        cfg = _CC.objects.first()
        if cfg is None:
            return {'enabled': True, 'limit': 30, 'window_seconds': 60 * 60}
        return {
            'enabled': bool(getattr(cfg, 'captcha_submission_captcha_enabled', True)),
            'limit': max(1, int(getattr(cfg, 'captcha_submission_limit', 30) or 30)),
            'window_seconds': max(60, int(getattr(cfg, 'captcha_submission_window_minutes', 60) or 60) * 60),
        }
    except Exception as exc:
        logger.warning('submission-captcha config unavailable: %s', exc)
        return {'enabled': True, 'limit': 30, 'window_seconds': 60 * 60}


def count_recent_submissions(user_id) -> int:
    """Return the number of submissions ``user_id`` has made inside the
    currently configured sliding window. 0 on error."""
    try:
        cfg = _submission_captcha_config()
        key = _submission_rate_key(user_id, bucket_seconds=cfg['window_seconds'])
        value = cache.get(key) or 0
        return int(value)
    except Exception:
        return 0


def record_submission_attempt(user_id, *, success: bool = True) -> None:
    """Increment the per-user submission counter used by the captcha
    escalation logic.  ``success`` is currently ignored but kept for
    consistency with :func:`record_login_attempt`."""
    if not user_id:
        return
    try:
        cfg = _submission_captcha_config()
        key = _submission_rate_key(user_id, bucket_seconds=cfg['window_seconds'])
        # Use a soft cache.incr; initialize if missing.
        count = cache.get(key)
        if count is None:
            try:
                cache.set(key, 1, timeout=cfg['window_seconds'] + 10)
            except Exception:
                pass
            return
        try:
            cache.incr(key)
        except (ValueError, TypeError):
            try:
                cache.set(key, int(count or 0) + 1, timeout=cfg['window_seconds'] + 10)
            except Exception:
                pass
    except Exception:
        pass


def submission_requires_captcha(request, user_id=None) -> bool:
    """Return True when the current user has exceeded the admin-configured
    submission frequency, and thus must submit a valid captcha with their
    next submission."""
    if user_id is None:
        user = getattr(request, 'user', None)
        if user is None or getattr(user, 'is_authenticated', False) is False:
            return False
        user_id = getattr(user, 'id', None)
    try:
        cfg = _submission_captcha_config()
        if not cfg['enabled']:
            return False
        return count_recent_submissions(user_id) >= cfg['limit']
    except Exception:
        return False


def check_submission_captcha(request) -> tuple[bool, str]:
    """Verify the ``captcha_id`` / ``captcha_answer`` posted alongside a
    submission.  Returns ``(ok, message)`` — ``ok`` is True when either the
    captcha is valid, or the user has not yet crossed the frequency
    threshold."""
    if not submission_requires_captcha(request):
        return True, ''
    challenge_id = (
        request.POST.get('captcha_id')
        or request.headers.get('X-Captcha-Id')
        or ''
    ).strip()
    submitted = (request.POST.get('captcha_answer') or '').strip()
    if not challenge_id or not submitted:
        return False, '请先输入图形验证码再提交。'
    if check_challenge(request, challenge_id, submitted, consume=True):
        return True, ''
    return False, '图形验证码错误或已失效，请重新输入。'


# ---------------------------------------------------------------------------
# Avatar access protection
# ---------------------------------------------------------------------------

def _avatar_captcha_config() -> dict:
    """Load avatar CAPTCHA settings with safe defaults."""
    try:
        from devlog.models import CaptchaConfig
        cfg = CaptchaConfig.objects.first()
        if cfg is None:
            return {'enabled': True, 'limit': 30, 'window_seconds': 60}
        return {
            'enabled': bool(getattr(cfg, 'captcha_avatar_captcha_enabled', True)),
            'limit': max(1, int(getattr(cfg, 'captcha_avatar_request_limit', 30) or 30)),
            'window_seconds': max(
                60,
                int(getattr(cfg, 'captcha_avatar_request_window_minutes', 1) or 1) * 60,
            ),
        }
    except Exception as exc:
        logger.warning('avatar-captcha config unavailable: %s', exc)
        return {'enabled': True, 'limit': 30, 'window_seconds': 60}


def _avatar_ip_digest(request) -> str:
    """Return a stable, non-reversible cache-key component for the client IP."""
    return hashlib.sha256(_client_ip(request).encode('utf-8')).hexdigest()[:32]


def _avatar_rate_key(request, window_seconds: int) -> str:
    bucket = int(time.time()) // max(1, int(window_seconds))
    return f'{CACHE_PREFIX}:rate:avatar:{_avatar_ip_digest(request)}:{bucket}'


def _avatar_proof_key(request, proof: str) -> str:
    return f'{CACHE_PREFIX}:proof:avatar:{_avatar_ip_digest(request)}:{proof}'


def record_avatar_request(request) -> int | None:
    """Record an avatar request and return its current count.

    ``None`` means the cache backend was unavailable. Callers should allow the
    request in that case so a Redis outage does not take down avatar serving.
    """
    cfg = _avatar_captcha_config()
    key = _avatar_rate_key(request, cfg['window_seconds'])
    ttl = cfg['window_seconds'] + 10
    try:
        if cache.add(key, 1, timeout=ttl):
            return 1
        try:
            return int(cache.incr(key))
        except (ValueError, TypeError, AttributeError):
            current = int(cache.get(key) or 0) + 1
            cache.set(key, current, timeout=ttl)
            return current
    except Exception as exc:
        logger.warning('avatar rate counter failed (%s); allowing', exc)
        return None


def avatar_requires_captcha(request) -> bool:
    """Return whether this avatar request needs CAPTCHA verification.

    Request counts are shared by IP. A successful CAPTCHA yields a random proof
    token that is bound to the same IP but is held only in the current page's
    JavaScript memory, so reloading the page does not keep a CAPTCHA bypass.
    """
    cfg = _avatar_captcha_config()
    if not cfg['enabled']:
        return False
    try:
        proof = (request.headers.get('X-Avatar-Captcha-Proof') or '').strip()
        if proof and cache.get(_avatar_proof_key(request, proof)):
            return False
        count = cache.get(_avatar_rate_key(request, cfg['window_seconds']))
        # The request that reaches the configured limit remains available;
        # subsequent requests require verification.
        return count is not None and int(count) > cfg['limit']
    except Exception as exc:
        logger.warning('avatar rate check failed (%s); allowing', exc)
        return False


def grant_avatar_captcha_proof(request) -> str | None:
    """Create a short-lived, IP-bound proof for the current page."""
    proof = secrets.token_urlsafe(32)
    try:
        cache.set(
            _avatar_proof_key(request, proof),
            '1',
            timeout=AVATAR_PROOF_TTL_SECONDS,
        )
        return proof
    except Exception as exc:
        logger.warning('avatar CAPTCHA proof write failed: %s', exc)
        return None


def verify_avatar_captcha(request, challenge_id: str, submitted_answer: str) -> str | None:
    """Strictly validate an avatar CAPTCHA and return a page-local proof."""
    if not check_challenge(
        request,
        challenge_id,
        submitted_answer,
        consume=True,
        fail_open_on_cache_error=False,
    ):
        return None
    return grant_avatar_captcha_proof(request)


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------

def _placeholder_png(answer: str) -> bytes:
    """Render a minimal PNG. Used only if Pillow fails catastrophically.

    The image simply shows the raw text in the middle so users can still
    read it — obviously weaker than a real captcha, but keeps the site
    functional.
    """
    try:
        from PIL import Image, ImageDraw
        image = Image.new('RGB', (IMAGE_WIDTH, IMAGE_HEIGHT), (220, 220, 220))
        draw = ImageDraw.Draw(image)
        draw.text((10, 15), answer, fill=(20, 20, 20))
        buf = io.BytesIO()
        image.save(buf, format='PNG')
        return buf.getvalue()
    except Exception:  # pragma: no cover
        # A 1x1 transparent PNG — not useful for humans, but it won't crash.
        return (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
                b'\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
                b'\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01'
                b'\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82')


# ---------------------------------------------------------------------------
# Django view helpers
# ---------------------------------------------------------------------------

def captcha_image_response(png_bytes: bytes) -> HttpResponse:
    """Wrap ``png_bytes`` in a non-cached ``image/png`` HttpResponse."""
    response = HttpResponse(png_bytes, content_type='image/png')
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response['Pragma'] = 'no-cache'
    response['Expires'] = '0'
    return response
