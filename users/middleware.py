"""Enforcement middleware.

Blocks requests that come from:
    * a banned IP (``users.models.IpBan``)
    * a banned user (``User.is_permanently_banned`` or ``User.banned_until``)
    * a user whose ``login`` feature is disabled

For HTML requests made by a logged-in user who becomes banned mid-session, we
log them out and redirect to ``/users/login/`` with a JSON payload stored in
the session so the shared punishment modal pops up immediately on the login
page (instead of returning a bare 403).
"""
from __future__ import annotations

import ipaddress
import json
import logging

from django.http import HttpResponse, HttpResponseForbidden, JsonResponse, HttpResponseRedirect
from django.contrib.auth import logout as _auth_logout
from django.contrib.auth import REDIRECT_FIELD_NAME as _NEXT_FIELD
from django.utils import timezone
from django.utils.deprecation import MiddlewareMixin

from .captcha import (
    check_challenge as _captcha_check,
    CAPTCHA_ON_ALL_POST as _captcha_all_post_cfg,
    _client_ip as _client_ip,  # reuse shared logic
)

logger = logging.getLogger(__name__)

LOGIN_URL = '/users/login/'


# ---------------------------------------------------------------------------
# Helpers (also used from views directly)
# ---------------------------------------------------------------------------

def ip_is_banned(ip: str) -> tuple[bool, str]:
    """Return ``(banned, reason_text)`` for a given IP."""
    if not ip:
        return False, ''
    try:
        from .models import IpBan
    except Exception:
        return False, ''
    try:
        entry = IpBan.objects.filter(ip_address=ip).first()
    except Exception:
        entry = None
    if entry and entry.is_active:
        return True, entry.reason or ''
    return False, ''


def user_is_banned(user) -> tuple[bool, str]:
    if user is None or getattr(user, 'is_anonymous', True):
        return False, ''
    try:
        if getattr(user, 'is_permanently_banned', False):
            return True, getattr(user, 'banned_reason', '') or '账号已被永久封禁'
        banned_until = getattr(user, 'banned_until', None)
        if banned_until and banned_until > timezone.now():
            return True, getattr(user, 'banned_reason', '') or '账号已被临时封禁'
    except Exception:
        return False, ''
    return False, ''


def user_feature_disabled(user, feature: str) -> bool:
    """Shortcut: ``user.feature_disabled(feature)`` but gracefully handles
    missing attributes (e.g., AnonymousUser or custom user models)."""
    if user is None or getattr(user, 'is_anonymous', True):
        return False
    fn = getattr(user, 'feature_disabled', None)
    if callable(fn):
        try:
            return bool(fn(feature))
        except Exception:
            return False
    return False


def _banned_user_punishment(user) -> dict:
    """Build a JSON-safe punishment payload for ``user``, or ``{}``."""
    if user is None or getattr(user, 'is_anonymous', True):
        return {}
    out = {
        'kind': None,
        'title': '',
        'reason': '账号已被封禁',
        'ends_at': None,
        'features': [],
        'feature_labels': [],
        'username': getattr(user, 'username', ''),
    }
    try:
        if getattr(user, 'is_permanently_banned', False):
            out['kind'] = 'permanent_ban'
            out['title'] = '账号已被永久封禁'
            reason = (getattr(user, 'banned_reason', '') or '').strip()
            if reason:
                out['reason'] = reason
            return out
        banned_until = getattr(user, 'banned_until', None)
        if banned_until and banned_until > timezone.now():
            out['kind'] = 'temporary_ban'
            out['title'] = '账号已被临时封禁'
            ends_local = timezone.localtime(banned_until)
            out['ends_at'] = ends_local.strftime('%Y-%m-%d %H:%M:%S')
            reason = (getattr(user, 'banned_reason', '') or '').strip()
            if reason:
                out['reason'] = reason
            return out
    except Exception:
        return {}
    return {}


def _wants_html(request) -> bool:
    accept = request.META.get('HTTP_ACCEPT', '') or ''
    # Treat text/html or wildcard browser accept headers as HTML requests.
    return 'html' in accept.lower() or accept == '' or accept.startswith('*/*')


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

class EnforcementMiddleware(MiddlewareMixin):
    """Checks IP / user bans before normal Django handling runs."""

    # URL name prefixes that bypass the ban check so users can still e.g.
    # see a "your account is banned" page. Keep tiny; any view you want
    # whitelisted should live under these namespaces.
    WHITELISTED_URL_NAMES = {
        'home',
        'devlog:status',
        'users:captcha_image',
        'captcha_image',
    }

    # Path prefixes for Django's built-in admin — we never want an admin
    # who accidentally banned themselves to lose access.
    ADMIN_PREFIXES = ('/admin', '/dadmin', '/djadmin')

    # Bypass ban checks for these paths (pattern match by startswith).
    BYPASS_PATH_PREFIXES = (
        '/static/', '/media/', '/favicon.ico',
        '/users/logout/', LOGIN_URL,
    )

    def process_request(self, request):
        # ------ Short-circuits ------
        path = getattr(request, 'path', '') or ''
        # Expose the punishment-notice session value to templates so the
        # shared modal in ``base.html`` can render it.
        try:
            session = request.session
            notice = session.get('punishment_notice') or ''
        except Exception:
            notice = ''
        request.session_punishment_notice = notice
        if path.startswith(self.BYPASS_PATH_PREFIXES):
            return None
        if any(path.startswith(p) for p in self.ADMIN_PREFIXES):
            # Staff users can still use the admin even if a feature is
            # disabled for normal users; we only block IP bans there.
            return None
        url_name = getattr(request.resolver_match, 'url_name', None) \
            if getattr(request, 'resolver_match', None) else None
        if url_name in self.WHITELISTED_URL_NAMES:
            return None

        # ------ IP ban (applies to anonymous as well) ------
        banned, reason = ip_is_banned(_client_ip(request))
        if banned:
            return self._ip_ban_response(request, reason or '该 IP 已被封禁')

        # ------ Authenticated user ban ------
        user = getattr(request, 'user', None)
        if user and getattr(user, 'is_authenticated', False):
            banned_user, reason_user = user_is_banned(user)
            if banned_user:
                # Destroy the authenticated session before creating the
                # short-lived notice session, preventing session reuse.
                try:
                    _auth_logout(request)
                    request.session.flush()
                except Exception:
                    pass
                return self._user_ban_redirect(request, user, reason_user)

            # Prevent users whose `login` feature is disabled from issuing
            # requests (they shouldn't be logged in at all; this is defense
            # in depth).
            if user_feature_disabled(user, 'login'):
                try:
                    _auth_logout(request)
                    request.session.flush()
                except Exception:
                    pass
                return self._user_ban_redirect(
                    request, user, '该账号已被限制登录，请联系管理员'
                )

        # ------ "All POST must have a valid captcha" mode ------
        if request.method == 'POST':
            if _call_cfg_bool(_captcha_all_post_cfg):
                challenge_id = request.POST.get('captcha_id') or request.META.get(
                    'HTTP_X_CAPTCHA_ID') or ''
                submitted = request.POST.get('captcha_answer') or ''
                if not challenge_id or not submitted:
                    return self._forbid('当前处于高风险模式，需要图形验证码')
                if not _captcha_check(request, challenge_id, submitted):
                    return self._forbid('图形验证码错误或已过期')

        return None

    # ------------------------------------------------------------------
    def _ip_ban_response(self, request, reason: str):
        if request.META.get('HTTP_ACCEPT', '').lower().startswith('application/json'):
            return JsonResponse({'ok': False, 'reason': reason}, status=403)
        return HttpResponseForbidden(
            '<html><body><h1>访问被拒绝</h1>'
            f'<p>{self._escape(reason)}</p>'
            f'<p>如需申诉请联系管理员。</p></body></html>',
            content_type='text/html; charset=utf-8',
        )

    def _user_ban_redirect(self, request, user, reason: str):
        """For HTML requests: redirect to ``/users/login/`` with a session
        notice that renders the punishment modal instead of a bare 403."""
        wants_html = _wants_html(request)
        if not wants_html:
            return JsonResponse({'ok': False, 'reason': reason}, status=403)
        # Build a punishment payload and stash it in the session so the
        # login view renders it in the shared modal JS.
        punishment = _banned_user_punishment(user)
        if not punishment.get('kind'):
            punishment = {
                'kind': 'temporary_ban',
                'title': '账号已被封禁',
                'reason': reason,
                'ends_at': None,
                'features': [],
                'feature_labels': [],
                'username': getattr(user, 'username', ''),
            }
        try:
            session = request.session
            session['punishment_notice'] = json.dumps(punishment, ensure_ascii=False)
            try:
                session.save()
            except Exception:
                pass
        except Exception:
            pass

        # Preserve the original path as `?next=` so once the account is
        # reinstated the user ends up on the intended page.
        redirect_to = LOGIN_URL
        try:
            original_path = (getattr(request, 'get_full_path', None) or
                             (lambda: getattr(request, 'path', '')))()
            if original_path and original_path not in ('', LOGIN_URL):
                sep = '&' if '?' in redirect_to else '?'
                redirect_to = f'{redirect_to}{sep}{_NEXT_FIELD}={original_path}'
        except Exception:
            pass
        return HttpResponseRedirect(redirect_to)

    def _forbid(self, reason: str):
        return HttpResponseForbidden(
            f'<html><body><h1>访问被拒绝</h1><p>{self._escape(reason)}</p></body></html>',
            content_type='text/html; charset=utf-8',
        )

    @staticmethod
    def _escape(text: str) -> str:
        return (text or '').replace('<', '&lt;').replace('>', '&gt;')


def _call_cfg_bool(cfg_callable) -> bool:
    """Evaluate either a plain bool or a callable returning a bool."""
    try:
        if callable(cfg_callable):
            return bool(cfg_callable())
        return bool(cfg_callable)
    except Exception:
        return False
