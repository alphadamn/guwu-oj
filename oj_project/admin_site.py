"""Captcha-protected Django admin login.

The default ``admin.site`` keeps every registered model; we only swap its
``login`` handler for a captcha-aware one so brute-force attempts on the admin
escalate to image + ALTCHA after the same per-IP failure threshold as the
public login.

Two-factor authentication is integrated at the same step:
* If the user has 2FA enabled, the password is verified but the user is NOT
  logged in. Instead, the user id + ``next`` URL are stashed in the session
  and the request is redirected to ``/users/2fa/verify/`` to complete the
  challenge.
* Staff users without 2FA configured are bounced to ``/users/2fa/setup/`` by
  the staff-2FA middleware before they can do anything in admin.
"""
from __future__ import annotations

import types

from django.contrib import admin
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.views import LoginView
from django.http import HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext as _

from users.captcha import (
    get_current_challenge_id,
    login_requires_captcha,
    record_login_attempt,
)
from users.forms import AdminLoginFormWithCaptcha


class AdminCaptchaLoginView(LoginView):
    template_name = 'admin/login.html'
    authentication_form = AdminLoginFormWithCaptcha

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['request'] = self.request
        kwargs['captcha_required'] = login_requires_captcha(self.request)
        return kwargs

    def form_invalid(self, form):
        record_login_attempt(self.request, success=False)
        # Show the captcha immediately after the first failure (the form was
        # built before this attempt was recorded).
        if login_requires_captcha(self.request) and 'captcha_id' not in form.fields:
            form = self.get_form()
        return super().form_invalid(form)

    def form_valid(self, form):
        user = form.get_user()
        record_login_attempt(self.request, success=True)
        # 2FA: if enabled, defer the actual login to the verify view. The
        # session-stashed user id lets that view resolve the user without
        # re-prompting for the password.
        if user and getattr(user, 'has_two_factor', False):
            self.request.session['two_factor_pending_user_id'] = str(user.pk)
            self.request.session['two_factor_pending_next'] = (
                self.request.POST.get(REDIRECT_FIELD_NAME)
                or self.request.GET.get(REDIRECT_FIELD_NAME)
                or reverse('admin:index')
            )
            return redirect('two_factor_verify')
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['captcha_required'] = login_requires_captcha(self.request)
        context['captcha_url'] = reverse('captcha_image')
        context['altcha_url'] = reverse('captcha_altcha')
        context['captcha_id'] = get_current_challenge_id(self.request) or ''
        return context


def _admin_login(self, request, extra_context=None):
    """Drop-in replacement for ``AdminSite.login`` using the captcha view."""
    if request.method == "GET" and self.has_permission(request):
        index_path = reverse("admin:index", current_app=self.name)
        return HttpResponseRedirect(index_path)

    context = {
        **self.each_context(request),
        "title": _("Log in"),
        "subtitle": None,
        "app_path": request.get_full_path(),
        "username": request.user.get_username(),
    }
    if REDIRECT_FIELD_NAME not in request.GET and REDIRECT_FIELD_NAME not in request.POST:
        context[REDIRECT_FIELD_NAME] = reverse("admin:index", current_app=self.name)
    context.update(extra_context or {})

    request.current_app = self.name
    return AdminCaptchaLoginView.as_view(extra_context=context)(request)


def patch_admin_login() -> None:
    """Bind the captcha login view onto the default admin site."""
    admin.site.login = types.MethodType(_admin_login, admin.site)
