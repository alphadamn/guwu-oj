"""Helpers for runtime email configuration.

Exposes :func:`apply_email_settings` and :func:`get_connection` so any code that
sends email can simply ::

    from devlog.email_config_helpers import apply_email_settings, get_connection
    apply_email_settings()  # updates django.conf.settings.*
    msg = EmailMultiAlternatives(...)
    msg.connection = get_connection()
    msg.send()

or, for Django's convenience helpers (``send_mail``, ``mail_managers``, ``mail_admins``) just call
:func:`apply_email_settings` first; they'll open a fresh connection using the overridden values.
"""
from __future__ import annotations

import logging
from typing import List

from django.conf import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def email_config():
    """Return the current :class:`~devlog.models.EmailConfig` row.

    Uses ``get_or_create(pk=1)`` so the singleton always exists after the
    first call — this mirrors how the other ``_Singleton`` configs are used
    in ``devlog/views.py`` / ``users/captcha.py``. Returns ``None`` only when
    the DB is unreachable / migrations pending.
    """
    try:
        from devlog.models import EmailConfig
        obj, _ = EmailConfig.objects.get_or_create(pk=1)
        return obj
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning('email_config() failed: %s', exc)
        return None


def apply_email_settings() -> None:
    """Push :class:`~devlog.models.EmailConfig` into ``django.conf.settings``.

    Called before every send so that admin edits take effect without a
    gunicorn restart. ``email_host_password`` is treated specially: the
    environment variable ``EMAIL_HOST_PASSWORD`` is the canonical source of
    truth; the database field is only used as an *override* when it is
    explicitly non-empty, so a commit to ``.env`` never has to wait on a DB
    change.
    """
    cfg = email_config()
    if cfg is None:
        return

    new_backend = (cfg.email_backend or '').strip() or settings.EMAIL_BACKEND
    new_host = (cfg.email_host or '').strip() or settings.EMAIL_HOST
    new_port = int(cfg.email_port or 0) or settings.EMAIL_PORT
    new_user = (cfg.email_host_user or '').strip() or settings.EMAIL_HOST_USER
    # Prefer the env var (canonical), fall back to the DB field when the admin
    # explicitly set one; this keeps secrets in the .env file while still
    # allowing an operator to override the SMTP password from the admin UI.
    env_password = getattr(settings, 'EMAIL_HOST_PASSWORD', '') or ''
    db_password = (cfg.email_host_password or '').strip()
    new_password = db_password or env_password
    new_timeout = int(cfg.email_timeout or 0) or getattr(settings, 'EMAIL_TIMEOUT', 20)

    try:
        settings.EMAIL_BACKEND = new_backend
        settings.EMAIL_HOST = new_host
        settings.EMAIL_PORT = new_port
        settings.EMAIL_HOST_USER = new_user or ''
        settings.EMAIL_HOST_PASSWORD = new_password or ''
        settings.EMAIL_USE_TLS = bool(cfg.email_use_tls)
        settings.EMAIL_USE_SSL = bool(cfg.email_use_ssl)
        settings.EMAIL_TIMEOUT = new_timeout
        # Default-from-email is used by django send_mail / mail_admins when from_email is None
        if cfg.default_from_email:
            settings.DEFAULT_FROM_EMAIL = cfg.default_from_email
        elif cfg.email_host_user and not settings.DEFAULT_FROM_EMAIL:
            # Reasonable default for sites that leave default_from_email empty
            settings.DEFAULT_FROM_EMAIL = cfg.email_host_user
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning('apply_email_settings mutation failed: %s', exc)


def effective_from_email() -> str:
    """Return a safe ``from_email`` string based on the current :class:`EmailConfig`."""
    cfg = email_config()
    if cfg is None:
        return getattr(settings, 'DEFAULT_FROM_EMAIL', '')
    if cfg.default_from_email:
        return cfg.default_from_email
    if cfg.email_host_user:
        return cfg.email_host_user
    return getattr(settings, 'DEFAULT_FROM_EMAIL', '')


def site_name_for_email() -> str:
    cfg = email_config()
    if cfg is None:
        return getattr(settings, 'SITE_NAME', 'Guwu Online Judge')
    return (cfg.site_name_for_email or '').strip() or 'Guwu Online Judge'


def admin_recipient_list() -> List[str]:
    """Return the list of admin email addresses from :class:`EmailConfig`."""
    cfg = email_config()
    if cfg is None:
        return []
    lines = (cfg.admin_recipients or '').splitlines()
    return [line.strip() for line in lines if '@' in (line or '')]


def get_connection(fail_silently: bool = False):
    """Open and return a new email connection using the current :class:`EmailConfig`.

    The connection is *not* closed automatically — the caller decides. Pass it to
    ``EmailMultiAlternatives`` / ``send_mail(connection=...)``.
    """
    apply_email_settings()
    from django.core.mail import get_connection as _dj_get_connection
    return _dj_get_connection(fail_silently=fail_silently)
