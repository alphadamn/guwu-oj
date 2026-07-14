import random
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags


def _apply_email_settings_from_admin():
    """Refresh Django's ``EMAIL_*`` / ``DEFAULT_FROM_EMAIL`` from the admin-managed row.

    Safe to call even if :class:`devlog.models.EmailConfig` is empty — a no-op in that case.
    """
    try:
        from devlog.email_config_helpers import apply_email_settings
        apply_email_settings()
    except Exception:
        pass


def _effective_from_email():
    try:
        from devlog.email_config_helpers import effective_from_email
        return effective_from_email()
    except Exception:
        return getattr(settings, 'DEFAULT_FROM_EMAIL', None)


def _site_name_for_email():
    try:
        from devlog.email_config_helpers import site_name_for_email
        return site_name_for_email()
    except Exception:
        return getattr(settings, 'OJ_SITE_NAME', 'Guwu OJ')


def _registration_config():
    """Return the active :class:`devlog.models.RegistrationConfig` singleton,
    or ``None`` when migrations are pending / DB unreachable.

    Callers should always read fields with ``getattr(cfg, 'field', default)``.
    """
    try:
        from devlog.models import RegistrationConfig
        return RegistrationConfig.objects.filter(pk=1).first()
    except Exception:
        return None


def _config_ttl_seconds() -> int:
    cfg = _registration_config()
    try:
        val = int(getattr(cfg, 'verification_code_ttl_seconds', 600))
        if val <= 0:
            val = 600
        return val
    except (TypeError, ValueError):
        return 600


def _config_code_length() -> int:
    cfg = _registration_config()
    try:
        val = int(getattr(cfg, 'verification_code_length', 6))
        if val < 4:
            val = 4
        return val
    except (TypeError, ValueError):
        return 6


def _config_rate_limit_per_hour() -> int:
    cfg = _registration_config()
    try:
        val = int(getattr(cfg, 'verification_rate_limit_per_hour', 5))
        if val < 1:
            val = 1
        return val
    except (TypeError, ValueError):
        return 5


def _generate_verification_code(length: int = None) -> str:
    """Generate a random numeric verification code.

    ``length`` falls back to ``RegistrationConfig.verification_code_length``
    when not provided, so the admin panel controls the digit count.
    """
    if length is None:
        length = _config_code_length()
    return ''.join(str(random.randint(0, 9)) for _ in range(length))


def _make_context(user_context: dict) -> dict:
    now = timezone.now()
    ttl_seconds = _config_ttl_seconds()
    base = {
        'current_year': now.year,
        'site_name': _site_name_for_email(),
        'expire_minutes': max(1, ttl_seconds // 60),
    }
    base.update(user_context)
    return base


def send_html_email(subject, template_name, context, to_email, from_email=None):
    """Send a multipart (HTML + plain-text) email.

    SMTP credentials, from address, backend, etc. are read from the admin-managed
    :class:`devlog.models.EmailConfig` row via :func:`apply_email_settings` —
    changes in admin take effect within the next email without a restart.
    """
    _apply_email_settings_from_admin()
    if from_email is None:
        from_email = _effective_from_email() or getattr(settings, 'DEFAULT_FROM_EMAIL', None)
    html_content = render_to_string(template_name, context)
    text_content = strip_tags(html_content)
    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_content,
        from_email=from_email,
        to=[to_email],
    )
    msg.attach_alternative(html_content, 'text/html')
    return msg.send(fail_silently=False)


def send_verification_code_email(to_email: str, code: str, **extra) -> int:
    """Send a 6-digit verification code for registration."""
    context = _make_context({
        'code': code,
        'email': to_email,
        'heading': '邮箱验证码',
    })
    return send_html_email(
        subject=f'[{context["site_name"]}] 你的邮箱验证码是 {code}',
        template_name='emails/verification_code.html',
        context=context,
        to_email=to_email,
    )


def send_password_reset_code_email(to_email: str, code: str, **extra) -> int:
    """Send a 6-digit verification code for password reset."""
    context = _make_context({
        'code': code,
        'email': to_email,
        'heading': '重置密码验证码',
    })
    return send_html_email(
        subject=f'[{context["site_name"]}] 你的重置密码验证码是 {code}',
        template_name='emails/password_reset_code.html',
        context=context,
        to_email=to_email,
    )


def send_password_reset_done_email(to_email: str, username: str = '') -> int:
    """Notify a user that their password has been reset."""
    context = _make_context({
        'email': to_email,
        'username': username,
        'heading': '密码已重置',
    })
    return send_html_email(
        subject=f'[{context["site_name"]}] 你的密码已成功重置',
        template_name='emails/password_reset_done.html',
        context=context,
        to_email=to_email,
    )


def send_welcome_email(to_email: str, username: str = '') -> int:
    """Welcome a newly-registered user."""
    context = _make_context({
        'email': to_email,
        'username': username,
        'heading': '欢迎加入',
    })
    return send_html_email(
        subject=f'欢迎来到 {context["site_name"]}！',
        template_name='emails/welcome.html',
        context=context,
        to_email=to_email,
    )


# ---------- Redis-backed short-lived verification codes ----------

from django.core.cache import cache


# NOTE: VERIFY_CODE_TTL / PASSWORD_RESET_CODE_TTL were previously hard-coded
# module constants. They are now driven by ``RegistrationConfig.verification_code_ttl_seconds``.
# The values below are kept for backward compatibility only.
VERIFY_CODE_TTL = 10 * 60
PASSWORD_RESET_CODE_TTL = 10 * 60

VERIFY_PREFIX = 'verify_code:'
PASSWORD_RESET_PREFIX = 'password_reset_code:'
SEND_RATE_PREFIX = 'send_rate:'
SEND_RATE_TTL = 60  # 1 email per minute per email address — 1 min threshold
SEND_RATE_HOURLY_PREFIX = 'send_rate_hour:'


def _make_cache_key(prefix: str, email: str) -> str:
    return f'{prefix}{email.lower().strip()}'


def can_send_code(email: str) -> bool:
    """Check that the email address has not hit the per-minute or
    per-hour rate limit.

    The per-hour limit is read from ``RegistrationConfig`` (admin-editable).
    """
    if cache.get(_make_cache_key(SEND_RATE_PREFIX, email)) is not None:
        return False
    # Per-hour sliding-window count.
    hour_key = _make_cache_key(SEND_RATE_HOURLY_PREFIX, email)
    hour_count = cache.get(hour_key)
    if hour_count is not None:
        try:
            if int(hour_count) >= _config_rate_limit_per_hour():
                return False
        except (TypeError, ValueError):
            pass
    return True


def _bump_rate_limit(email: str):
    """Increment the per-hour send counter for ``email``.

    We use Redis' INCR semantics through Django's cache layer — if
    the key doesn't exist, ``incr`` would throw on some backends, so
    we initialise it with ``add`` first.
    """
    hour_key = _make_cache_key(SEND_RATE_HOURLY_PREFIX, email)
    try:
        # add() returns True when the key was newly created.
        if not cache.add(hour_key, 1, timeout=3600):
            try:
                cache.incr(hour_key)
            except Exception:
                pass
    except Exception:
        pass


def issue_verification_code(email: str) -> str:
    code = _generate_verification_code()
    cache.set(
        _make_cache_key(VERIFY_PREFIX, email),
        code,
        timeout=_config_ttl_seconds(),
    )
    cache.set(
        _make_cache_key(SEND_RATE_PREFIX, email),
        '1',
        timeout=SEND_RATE_TTL,
    )
    _bump_rate_limit(email)
    return code


def issue_password_reset_code(email: str) -> str:
    code = _generate_verification_code()
    cache.set(
        _make_cache_key(PASSWORD_RESET_PREFIX, email),
        code,
        timeout=_config_ttl_seconds(),
    )
    cache.set(
        _make_cache_key(SEND_RATE_PREFIX, email),
        '1',
        timeout=SEND_RATE_TTL,
    )
    _bump_rate_limit(email)
    return code


def check_verification_code(email: str, code: str) -> bool:
    key = _make_cache_key(VERIFY_PREFIX, email)
    stored = cache.get(key)
    if stored is None:
        return False
    if secrets.compare_digest(str(stored), str(code).strip()):
        # Consume the code on successful verification.
        cache.delete(key)
        return True
    return False


def check_password_reset_code(email: str, code: str) -> bool:
    key = _make_cache_key(PASSWORD_RESET_PREFIX, email)
    stored = cache.get(key)
    if stored is None:
        return False
    if secrets.compare_digest(str(stored), str(code).strip()):
        # Consume the code on successful verification.
        cache.delete(key)
        return True
    return False
