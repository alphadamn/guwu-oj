from django.http import HttpResponse, Http404, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import auth
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import reverse_lazy, reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.contrib import messages

from django.utils.http import http_date
from django_ratelimit.decorators import ratelimit
from django.core.cache import cache
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_protect
import json
import time
import logging

from .forms import (
    UserRegisterForm,
    UserUpdateForm,
    SendVerificationCodeForm,
    PasswordResetRequestForm,
    PasswordResetForm,
    LoginFormWithCaptcha,
    TwoFactorSetupForm,
    TwoFactorVerifyForm,
    TwoFactorReauthForm,
    TwoFactorDisableForm,
)
from .models import User
from .email_utils import (
    can_send_code,
    issue_verification_code,
    issue_password_reset_code,
    send_verification_code_email,
    send_password_reset_code_email,
    send_password_reset_done_email,
    send_welcome_email,
    consume_verification_code,
)
from .captcha import (
    generate_challenge,
    captcha_image_response,
    login_requires_captcha,
    record_login_attempt,
    get_current_challenge_id,
    record_avatar_request,
    avatar_requires_captcha,
    verify_avatar_captcha,
    _client_ip,
)
from .altcha import generate_challenge as generate_altcha_challenge

logger = logging.getLogger(__name__)


# Session keys used by the two-factor / staff-reauth flow. Keeping them in
# one place makes it easier to audit and to flush after a successful login.
TWO_FACTOR_PENDING_USER_KEY = 'two_factor_pending_user_id'
TWO_FACTOR_PENDING_NEXT_KEY = 'two_factor_pending_next'
TWO_FACTOR_VERIFIED_KEY = 'two_factor_verified_at'  # timestamp (seconds)
TWO_FACTOR_SETUP_SECRET_KEY = 'two_factor_setup_secret'  # plaintext secret during setup
# v2: renamed so sudo stamps that older code granted at login time (still
# living in existing sessions until their TTL expired) become invalid at
# once — those sessions would otherwise silently pass sensitive-operation
# checks without a fresh explicit TOTP challenge.
STAFF_REAUTH_AT_KEY = 'staff_reauth_at_v2'  # timestamp (seconds)
STAFF_REAUTH_NEXT_KEY = 'staff_reauth_next'
# The admin path this sudo stamp was verified for (see ``staff_reauth_valid``):
# the stamp authorizes only that operation, so verifying and then canceling
# out does NOT leave a blanket sudo grant for other sensitive operations.
STAFF_REAUTH_TARGET_KEY = 'staff_reauth_target'

# How long a successful staff-reauth ("sudo" mode) lasts before another 2FA
# challenge is required for destructive admin actions. Imported from settings
# so deployments can tune it.
def _staff_reauth_ttl() -> int:
    from django.conf import settings
    return int(getattr(settings, 'STAFF_REAUTH_TTL_SECONDS', 30 * 60))


# ---- Config helpers (graceful when DB / migrations are not ready yet) -----


def _load_registration_config():
    try:
        from devlog.models import RegistrationConfig
        return RegistrationConfig.objects.filter(pk=1).first()
    except Exception:
        return None


def registration_enabled() -> bool:
    cfg = _load_registration_config()
    if cfg is None:
        return True
    return bool(getattr(cfg, 'registration_enabled', True))


def email_verification_required() -> bool:
    cfg = _load_registration_config()
    if cfg is None:
        return True
    return bool(getattr(cfg, 'email_verification_required', True))


def _punishment_for_user(user) -> dict:
    """Shortcut around :func:`User.punishment_info` that never raises.

    Returns ``None`` when the user has no active punishment.
    """
    try:
        return user.punishment_info()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning('punishment_info() failed for user=%s: %s', user, exc)
        return None


def _punishment_for_view(punishment: dict, user) -> dict:
    """Return a JSON-safe dict augmented with display-friendly strings."""
    out = dict(punishment or {})
    out.setdefault('username', getattr(user, 'username', ''))
    return out


def _login_failure_reason(form) -> str:
    """Map a failed login form's errors to a single Chinese reason.

    The login template renders neither ``form.non_field_errors()`` nor the
    per-field error lists, so without this the page just re-renders with no
    explanation. Django's ``AuthenticationForm`` tags credential failures
    with ``code='invalid_login'`` and disabled accounts with
    ``code='inactive'``; the captcha mixin raises plain (Chinese) messages.
    We surface the most actionable error first.
    """
    if form is None or not getattr(form, 'errors', None):
        return '登录失败，请重试。'
    data = form.errors.as_data()
    non_field = data.get('__all__') or []
    # Captcha mistakes are the most actionable (refresh + retype) — surface
    # their message verbatim before credential/inactive reasons.
    for err in non_field:
        for msg in getattr(err, 'messages', []) or []:
            if '验证码' in str(msg):
                return str(msg)
    # Django auth sets explicit codes on credential / inactive errors.
    for err in non_field:
        code = getattr(err, 'code', '') or ''
        if code == 'invalid_login':
            return '用户名或密码错误，请重试。'
        if code == 'inactive':
            return '该账户已被停用，请联系管理员。'
    # A field-level error on username/password usually means bad credentials
    # (e.g. the required-field check fired before auth ran).
    if data.get('username') or data.get('password'):
        return '用户名或密码错误，请重试。'
    parts = [
        str(m)
        for errs in data.values()
        for err in errs
        for m in (getattr(err, 'messages', []) or [])
    ]
    return ' '.join(parts) or '登录失败，请重试。'


# ---- Views -----------------------------------------------------------------


class CustomLoginView(LoginView):
    """Login view that escalates to a captcha after one failed attempt, and
    shows a detailed punishment popup for accounts with active punishments.

    Behaviour:

    * **permanent_ban / temporary_ban** — the user is NOT logged in; the
      login page re-renders with a modal describing the ban.
    * **feature_ban** — the user IS allowed to log in, and we stash
      ``request.session['punishment_notice']`` so the next page load renders
      an informational modal listing the disabled features / deadline.
    """
    template_name = 'users/login.html'
    redirect_authenticated_user = True
    authentication_form = LoginFormWithCaptcha

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['request'] = self.request
        kwargs['captcha_required'] = login_requires_captcha(self.request)
        return kwargs

    def form_invalid(self, form):
        record_login_attempt(self.request, success=False)
        # Surface *why* the login failed via the messages framework. The
        # login template does not render form errors, so without this a bad
        # password looks indistinguishable from a network hiccup. Capture the
        # reason from the submitted (bound) form BEFORE we potentially
        # rebuild it — a fresh form carries no errors.
        messages.error(self.request, _login_failure_reason(form))
        # ``login_requires_captcha`` now reflects the incremented failure
        # count, but the *form* above was built before this failure was
        # recorded, so it may lack the captcha fields. Rebuild it so the
        # re-rendered page shows the captcha immediately after the very first
        # failure (instead of lagging one extra round-trip behind).
        if login_requires_captcha(self.request) and 'captcha_id' not in form.fields:
            form = self.get_form()
        return super().form_invalid(form)

    def _next_url(self) -> str:
        """Return a same-origin ``next`` URL, if one was supplied."""
        candidate = (
            self.request.POST.get(auth.REDIRECT_FIELD_NAME, '').strip()
            or self.request.GET.get(auth.REDIRECT_FIELD_NAME, '').strip()
        )
        if candidate and url_has_allowed_host_and_scheme(
            candidate,
            allowed_hosts={self.request.get_host()},
            require_https=self.request.is_secure(),
        ):
            return candidate
        return ''

    def form_valid(self, form):
        user = form.get_user()
        punishment = _punishment_for_user(user)

        # 1) Hard ban — never log the user in. Render the login page back
        #    with the shared modal describing the ban (punishment_json is
        #    injected by ``get_context_data``). We preserve ``next`` so
        #    admins / redirected users can still return to their intended
        #    page once the account is reinstated.
        if punishment and punishment['kind'] in ('permanent_ban', 'temporary_ban'):
            record_login_attempt(self.request, success=False)
            context = self.get_context_data(form=form)
            context['captcha_required'] = (
                bool(context.get('form'))
                and 'captcha_id' in getattr(context['form'], 'fields', {})
            )
            context['captcha_url'] = reverse('captcha_image')
            context['altcha_url'] = reverse('captcha_altcha')
            next_url = self._next_url()
            if next_url:
                context['next'] = next_url
            return self.render_to_response(context)

        # 2) Feature ban — allow login but show details via session notice.
        if punishment and punishment['kind'] == 'feature_ban':
            self.request.session['punishment_notice'] = json.dumps(
                _punishment_for_view(punishment, user), ensure_ascii=False,
            )
        else:
            self.request.session.pop('punishment_notice', None)

        # 3) Two-factor authentication — if the user has 2FA enabled, do
        #    NOT log them in yet. Stash the user id + ``next`` in the
        #    session and redirect to the 2FA verify view, which will
        #    finish the login flow once a valid TOTP code is supplied.
        #    The password itself was valid, so the captcha failure counter
        #    is reset (we only escalate on real password failures). The
        #    verify view enforces its own rate limit to prevent 2FA
        #    brute-forcing.
        if user.has_two_factor:
            record_login_attempt(self.request, success=True)
            self.request.session[TWO_FACTOR_PENDING_USER_KEY] = str(user.pk)
            self.request.session[TWO_FACTOR_PENDING_NEXT_KEY] = self._next_url()
            self.request.session.pop(TWO_FACTOR_VERIFIED_KEY, None)
            return redirect('two_factor_verify')

        record_login_attempt(self.request, success=True)
        user.last_login_ip = _client_ip(self.request)
        user.save(update_fields=['last_login_ip'])
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = context.get('form')
        context['captcha_required'] = (
            bool(form) and 'captcha_id' in getattr(form, 'fields', {})
        )
        context['captcha_url'] = reverse('captcha_image')
        context['altcha_url'] = reverse('captcha_altcha')
        # Make sure ``next`` is available to the template's hidden input so
        # redirects survive a hard-ban re-render.
        if not context.get('next'):
            next_url = self._next_url()
            if next_url:
                context['next'] = next_url

        # Inject punishment info when the submitting user (or the already
        # authenticated ``request.user``) has an active punishment. This
        # runs for both valid and invalid form submissions so the modal
        # appears regardless of password / captcha correctness. It also
        # picks up a session-stashed punishment notice set by the
        # enforcement middleware when a logged-in user is banned mid-session.
        user_for_punishment = None
        if form is not None and hasattr(form, 'get_user'):
            user_for_punishment = form.get_user()
        if user_for_punishment is None:
            request_user = getattr(self.request, 'user', None)
            if request_user is not None and getattr(request_user, 'is_authenticated', False):
                user_for_punishment = request_user

        session_punishment_raw = None
        try:
            session_punishment_raw = self.request.session.get(
                'punishment_notice', ''
            ) or ''
        except Exception:
            session_punishment_raw = ''
        session_punishment = None
        if session_punishment_raw:
            try:
                session_punishment = json.loads(session_punishment_raw)
            except (TypeError, ValueError):
                session_punishment = None

        punishment = None
        if user_for_punishment is not None:
            punishment = _punishment_for_user(user_for_punishment)
        # Fallback: use the session-stashed punishment (redirected by the
        # enforcement middleware) when no direct user is available — e.g.
        # when the middleware logs out a banned user mid-session and
        # redirects to /users/login/.
        if (not punishment or not punishment.get('kind')) and session_punishment:
            punishment = session_punishment

        if punishment and punishment.get('kind') in (
            'permanent_ban', 'temporary_ban', 'feature_ban',
        ):
            try:
                context['punishment_json'] = json.dumps(
                    _punishment_for_view(punishment, user_for_punishment),
                    ensure_ascii=False,
                )
            except (TypeError, ValueError):
                pass
        return context


class CustomLogoutView(LogoutView):
    next_page = reverse_lazy('home')


# ---- Captcha image endpoint -----------------------------------------------


@csrf_protect
@require_POST
def clear_punishment_notice(request):
    """Remove the displayed punishment notice from the current session."""
    request.session.pop('punishment_notice', None)
    return JsonResponse({'ok': True})


def captcha_image(request):
    """Return a fresh PNG captcha and remember the challenge id in the
    session. The challenge_id is *not* in the URL — avoiding trivial
    replay across pages.
    """
    try:
        challenge_id, _answer, png = generate_challenge(request)
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning('Captcha image generation failed: %s', exc)
        # Render a placeholder to avoid a broken <img>.
        from .captcha import _placeholder_png
        png = _placeholder_png('?')
        challenge_id = ''
        status=403
    else:
        status=200
    response = captcha_image_response(png, status=status)
    # Expose the challenge_id in a JS-readable header so the template
    # can populate a hidden input without a page reload.
    if challenge_id:
        response['X-Captcha-Id'] = challenge_id
    return response


def captcha_altcha(request):
    """Return a fresh ALTCHA proof-of-work challenge as JSON.

    Stateless and HMAC-signed, so no session/cache write is required on the
    issuing side. The client widget solves it and posts the base64 payload
    back alongside the image captcha.
    """
    challenge = generate_altcha_challenge()
    response = JsonResponse(challenge)
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response['Pragma'] = 'no-cache'
    response['Expires'] = '0'
    return response


@csrf_protect
def send_verification_code(request):
    """AJAX endpoint that sends a 6-digit verification code to an email address.

    When the admin has disabled email verification for registration, this
    endpoint still accepts requests so the UI can remain stateless — it just
    short-circuits with a success message (no email is actually sent).
    """
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'message': '仅支持 POST 请求。'}, status=405)

    if not email_verification_required():
        # Verification is off server-side — pretend success so the UI does
        # not loop trying to send a code. The registration form also
        # accepts an empty code when the feature is disabled.
        return JsonResponse({'ok': True, 'message': '已关闭邮箱验证。'})

    form = SendVerificationCodeForm(request.POST)
    if not form.is_valid():
        return JsonResponse(
            {'ok': False, 'message': '邮箱格式不正确。'},
            status=400,
        )

    email = form.cleaned_data['email']

    # Prevent duplicate emails and rate-limit.
    if not can_send_code(email):
        return JsonResponse(
            {'ok': False, 'message': '请求过于频繁，请稍后再试。'},
            status=429,
        )

    if User.objects.filter(email__iexact=email).exists():
        # Do not leak whether the email is registered; still return OK to
        # prevent user enumeration, but do not actually send an email.
        return JsonResponse(
            {'ok': True, 'message': '如果该邮箱未被注册，验证码已发送。'},
        )

    code = issue_verification_code(email)
    try:
        send_verification_code_email(email, code)
    except Exception as exc:  # pragma: no cover - network-side failure
        logger.exception('Failed to send verification email to %s: %s', email, exc)
        return JsonResponse(
            {'ok': False, 'message': '邮件发送失败，请稍后重试。'},
            status=500,
        )

    return JsonResponse(
        {'ok': True, 'message': '验证码已发送，请查收邮箱。有效期 10 分钟。'},
    )


def register(request):
    """Render or submit the user registration form.

    When :attr:`RegistrationConfig.registration_enabled` is False we show a
    disabled page instead, so the admin can close registrations quickly.
    """
    if not registration_enabled():
        messages.info(request, '注册当前已关闭。')
        return redirect('home')

    if request.method == 'POST':
        form = UserRegisterForm(request.POST, request=request)
        if form.is_valid():
            user = form.save()
            verification_code = form.cleaned_data.get('verification_code')
            if verification_code:
                consume_verification_code(user.email, verification_code)
            if user.referrer_id:
                from points.models import PointConfig
                from points.services import apply_points

                try:
                    config = PointConfig.get_solo()
                    if config.inviter_registration_points:
                        apply_points(
                            user_id=user.referrer_id,
                            amount=config.inviter_registration_points,
                            event_type='referral_inviter',
                            event_key=str(user.id),
                            description=f'邀请用户 {user.username} 注册',
                        )
                    if config.invitee_registration_points:
                        apply_points(
                            user_id=user.id,
                            amount=config.invitee_registration_points,
                            event_type='referral_invitee',
                            event_key=str(user.referrer_id),
                            description=f'通过 {user.referrer.username} 的邀请注册',
                        )
                except Exception:
                    logger.exception('Referral point award failed for registered user %s', user.id)
            login(request, user)
            try:
                send_welcome_email(user.email, username=user.username)
            except Exception:
                logger.exception('Welcome email failed for %s', user.email)
            messages.success(request, '注册成功！欢迎加入。')
            return redirect('home')
    else:
        form = UserRegisterForm(
            request=request,
            initial={'referral_code': request.GET.get('ref', '').strip()},
        )
    # Pass ``email_verification_required`` to the template so it can
    # conditionally show / hide the "send verification code" UI, plus
    # the current captcha challenge info so the image can be rendered.
    context = {
        'form': form,
        'email_verification_required': email_verification_required(),
        'captcha_required': 'captcha_id' in form.fields,
        'captcha_url': reverse('captcha_image'),
        'altcha_url': reverse('captcha_altcha'),
        'captcha_id': get_current_challenge_id(request) or '',
    }
    return render(request, 'users/register.html', context)


def password_reset_request(request):
    """Step 1: enter email to receive a verification code."""
    if request.method == 'POST':
        form = PasswordResetRequestForm(request.POST, request=request)
        if form.is_valid():
            email = form.cleaned_data['email']

            # Check whether the user exists; we silently succeed either way
            # to avoid leaking user enumeration info, but we only send an
            # email for existing addresses.
            if not can_send_code(email):
                messages.error(request, '请求过于频繁，请稍后再试。')
            else:
                try:
                    user = User.objects.get(email__iexact=email, is_active=True)
                    code = issue_password_reset_code(email)
                    send_password_reset_code_email(email, code)
                    logger.info('Password reset code sent for %s', email)
                except User.DoesNotExist:
                    logger.info('Password reset requested for unknown email: %s', email)
                except Exception:
                    logger.exception('Failed to send password reset email to %s', email)

            return redirect('password_reset_confirm', email=email)
    else:
        form = PasswordResetRequestForm(request=request)

    context = {
        'form': form,
        'captcha_required': 'captcha_id' in form.fields,
        'captcha_url': reverse('captcha_image'),
        'altcha_url': reverse('captcha_altcha'),
        'captcha_id': get_current_challenge_id(request) or '',
    }
    return render(request, 'users/password_reset_request.html', context)


@ratelimit(key='ip', rate='5/m', method='POST', block=False)
def password_reset_confirm(request, email):
    """Step 2: enter code + new password."""
    email = email.strip().lower()

    if request.method == 'POST' and getattr(request, 'limited', False):
        messages.error(request, '密码重置尝试过于频繁，请稍后再试。')
        form = PasswordResetForm(initial={'email': email}, request=request)
    elif request.method == 'POST':
        post = request.POST.copy()
        post['email'] = email
        form = PasswordResetForm(data=post, request=request)
        if form.is_valid():
            user = form.save()
            try:
                send_password_reset_done_email(user.email, username=user.username)
            except Exception:
                logger.exception('Password-reset-done email failed for %s', user.email)
            messages.success(request, '密码已成功重置，请使用新密码登录。')
            return redirect('login')
    else:
        form = PasswordResetForm(initial={'email': email}, request=request)

    context = {
        'form': form,
        'email': email,
        'captcha_required': 'captcha_id' in form.fields,
        'captcha_url': reverse('captcha_image'),
        'altcha_url': reverse('captcha_altcha'),
        'captcha_id': get_current_challenge_id(request) or '',
    }
    return render(request, 'users/password_reset_confirm.html', context)


@login_required
def profile(request, username):
    user = get_object_or_404(User, username=username)
    recent_submissions = user.submissions.select_related(
        'problem', 'contest_problem__contest',
    ).order_by('-created_at')[:10]
    return render(request, 'users/profile.html', {
        'user_profile': user,
        'recent_submissions': recent_submissions,
    })


@login_required
def points_center(request):
    from points.models import PointConfig, PointLedgerEntry

    referral_url = request.build_absolute_uri(
        f"{reverse('register')}?ref={request.user.referral_code}"
    )
    return render(request, 'users/points_center.html', {
        'points_config': PointConfig.get_solo(),
        'recent_entries': PointLedgerEntry.objects.filter(user=request.user)[:30],
        'referral_url': referral_url,
        'checkin_rewards': [
            ('第 1 天', PointConfig.get_solo().daily_checkin_day_1_points),
            ('第 2 天', PointConfig.get_solo().daily_checkin_day_2_points),
            ('第 3 天', PointConfig.get_solo().daily_checkin_day_3_points),
            ('第 4 天', PointConfig.get_solo().daily_checkin_day_4_points),
            ('第 5 天+', PointConfig.get_solo().daily_checkin_day_5_plus_points),
        ],
    })


@login_required
@require_POST
def daily_check_in(request):
    """Claim today's check-in explicitly from the frontend modal."""
    from points.services import check_in_notice, check_in_user

    try:
        checkin, created = check_in_user(user_id=request.user.id)
        payload = check_in_notice(checkin)
        payload['created'] = created
        return JsonResponse({'ok': True, **payload})
    except Exception:
        logger.exception('Daily check-in claim failed for user %s', request.user.id)
        return JsonResponse({'ok': False, 'message': '签到失败，请稍后重试。'}, status=500)


@login_required
@ratelimit(key='user', rate='3/m', method='POST')
def edit_profile(request):
    if request.method == 'POST':
        files = request.FILES if 'avatar' in request.FILES else None
        post_data = request.POST.copy()
        if 'avatar' not in request.FILES:
            post_data.pop('avatar', None)
        form = UserUpdateForm(post_data, files, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, '个人资料更新成功！')
            return redirect('profile', username=request.user.username)
    else:
        form = UserUpdateForm(instance=request.user)
    return render(request, 'users/edit_profile.html', {'form': form})


@login_required
def verify_avatar_captcha_view(request):
    """Verify the CAPTCHA used to unlock high-frequency avatar requests."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'message': '请使用 POST 请求。'}, status=405)
    challenge_id = (request.POST.get('captcha_id') or '').strip()
    answer = (request.POST.get('captcha_answer') or '').strip()
    altcha_payload = (request.POST.get('altcha') or '').strip()
    if not challenge_id or not answer:
        return JsonResponse({'ok': False, 'message': '请填写图形验证码。'}, status=400)
    if not altcha_payload:
        return JsonResponse({'ok': False, 'message': '验证失败，请刷新后重试。'}, status=400)
    proof = verify_avatar_captcha(request, challenge_id, answer, altcha_payload)
    if not proof:
        return JsonResponse({'ok': False, 'message': '图形验证码错误或已失效，请重新获取。'}, status=400)
    return JsonResponse({'ok': True, 'proof': proof, 'expires_in': 300})


def avatar(request, username):
    """Serve a user's avatar directly from the PostgreSQL database with caching."""
    user = get_object_or_404(User, username=username)
    if not user.has_avatar:
        raise Http404('No avatar stored for this user')

    blob = user.avatar_blob

    # Count by requester IP, not avatar username, so rotating usernames cannot
    # bypass the protection. The current request is allowed to establish the
    # threshold; later requests require a CAPTCHA proof.
    record_avatar_request(request)
    if avatar_requires_captcha(request):
        return JsonResponse({
            'captcha_required': True,
            'message': '头像访问过于频繁，请完成图形验证码。',
        }, status=429, headers={'X-Captcha-Required': '1'})

    cache_key = f'avatar_data:{username}'
    freq_key = f'avatar_freq:{username}'

    current_minute = int(time.time() // 60)
    minute_key = f'{freq_key}:{current_minute}'

    try:
        if cache.add(minute_key, 1, timeout=60):
            request_count = 1
        else:
            try:
                request_count = cache.incr(minute_key)
            except (ValueError, TypeError, AttributeError):
                request_count = int(cache.get(minute_key) or 0) + 1
                cache.set(minute_key, request_count, timeout=60)
    except Exception as exc:
        logger.warning('avatar cache frequency counter failed: %s', exc)
        request_count = 1

    should_cache = request_count >= 5

    def _avatar_response(data, content_type, cache_status):
        response = HttpResponse(data, content_type=content_type)
        response['Last-Modified'] = http_date(blob.updated_at.timestamp())
        # Prevent browser, reverse-proxy, and Nginx proxy_cache storage.
        response['Cache-Control'] = 'private, no-store, no-cache, must-revalidate, max-age=0'
        response['Pragma'] = 'no-cache'
        response['Expires'] = '0'
        response['X-Accel-Expires'] = '0'
        response['X-Cache'] = cache_status
        return response

    cached_data = cache.get(cache_key)
    if cached_data:
        cached_content_type, cached_image_data, cached_updated_at = cached_data
        if cached_updated_at == blob.updated_at.timestamp():
            return _avatar_response(cached_image_data, cached_content_type, 'HIT')

    image_data = blob.image_data
    content_type = blob.content_type
    updated_at = blob.updated_at.timestamp()

    if should_cache:
        cache.set(cache_key, (content_type, image_data, updated_at), timeout=3600)

    return _avatar_response(image_data, content_type, 'MISS')


# ---------------------------------------------------------------------------
# Two-factor authentication views
# ---------------------------------------------------------------------------

def _resolve_pending_2fa_user(request):
    """Return the User stashed in the session for the 2FA login flow, or None."""
    user_id = request.session.get(TWO_FACTOR_PENDING_USER_KEY)
    if not user_id:
        return None
    try:
        return User.objects.get(pk=user_id)
    except (User.DoesNotExist, ValueError):
        return None


def _complete_2fa_login(request, user, *, next_url=''):
    """Finish the login flow after a successful 2FA challenge."""
    # Setting ``auth.USER_AUTHENTICATED_SESSION`` via ``login()`` rotates
    # the session key, which invalidates the stashed user id *after* the
    # new session is in place. We therefore stash the timestamp on the
    # post-login session first, then perform ``login`` and re-mark it.
    request.session.pop(TWO_FACTOR_PENDING_USER_KEY, None)
    next_url = next_url or request.session.pop(TWO_FACTOR_PENDING_NEXT_KEY, '') or ''
    request.session[TWO_FACTOR_VERIFIED_KEY] = int(time.time())
    login(request, user)
    request.session[TWO_FACTOR_VERIFIED_KEY] = int(time.time())
    # NOTE: login-time 2FA deliberately does NOT grant the sudo stamp
    # (``STAFF_REAUTH_AT_KEY``). Sudo mode is single-use and must always be
    # earned through the dedicated ``two_factor_reauth`` challenge right
    # before a destructive operation, so every sensitive action presents an
    # explicit TOTP prompt.
    if not next_url or not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = ''
    return redirect(next_url or 'home')


@ratelimit(key='ip', rate='10/m', method='POST', block=False)
def two_factor_verify(request):
    """Login step 2: enter the 6-digit TOTP code (or a backup code)."""
    user = _resolve_pending_2fa_user(request)
    if user is None:
        messages.error(request, '两步验证会话已过期，请重新登录。')
        return redirect('login')

    # Staff users without 2FA configured are forced through the setup flow
    # rather than the verify flow — they cannot complete a code challenge
    # without a configured authenticator.
    if (user.is_staff or user.is_superuser) and not user.has_two_factor:
        return redirect(reverse('two_factor_setup') + '?forced=1')

    if request.method == 'POST' and getattr(request, 'limited', False):
        messages.error(request, '尝试次数过多，请稍后再试。')
        form = TwoFactorVerifyForm(user=user)
    elif request.method == 'POST':
        form = TwoFactorVerifyForm(request.POST, user=user)
        if form.is_valid():
            return _complete_2fa_login(request, user)
    else:
        form = TwoFactorVerifyForm(user=user)

    return render(request, 'users/two_factor_verify.html', {
        'form': form,
        'target_user': user,
        'has_backup_codes': bool(user.two_factor_backup_codes),
    })


@login_required
def two_factor_setup(request):
    """Generate a new TOTP secret, show a QR code, and verify the first code.

    Staff users who land here without 2FA configured are forced through this
    flow before they can reach destructive admin actions (see middleware).
    """
    user = request.user

    # Staff users without 2FA must complete setup before they can dismiss
    # this page and continue using admin. Standard users may cancel.
    forced = user.is_staff or user.is_superuser or request.GET.get('forced') == '1'

    # If the user already has 2FA enabled, redirect to the disable page or
    # show a "2FA already enabled" page. We allow re-setup only after an
    # explicit disable (so the secret rotates only when the user proves
    # they hold the current one).
    if user.has_two_factor:
        messages.info(request, '您已启用两步验证。如需更换设备，请先停用后再重新设置。')
        return redirect('two_factor_disable')

    if request.method == 'POST':
        # The plaintext secret lives in the session between the GET (which
        # renders the QR code) and the POST (which verifies the first code).
        secret = request.session.get(TWO_FACTOR_SETUP_SECRET_KEY) or ''
        form = TwoFactorSetupForm(request.POST, user=user, secret=secret)
        if form.is_valid():
            from .two_factor import (
                encrypt_secret, generate_backup_codes, hash_backup_codes,
            )
            user.two_factor_secret = encrypt_secret(secret)
            user.two_factor_enabled = True
            backup_codes = generate_backup_codes()
            user.two_factor_backup_codes = hash_backup_codes(backup_codes)
            user.two_factor_setup_required = False
            user.save(update_fields=[
                'two_factor_secret', 'two_factor_enabled',
                'two_factor_backup_codes', 'two_factor_setup_required',
            ])
            request.session.pop(TWO_FACTOR_SETUP_SECRET_KEY, None)
            # Mark this session as having completed 2FA so the middleware
            # doesn't immediately bounce them back into setup. This does NOT
            # grant the sudo stamp — destructive operations still require a
            # dedicated ``two_factor_reauth`` TOTP challenge each time.
            request.session[TWO_FACTOR_VERIFIED_KEY] = int(time.time())
            return render(request, 'users/two_factor_setup.html', {
                'form': None,
                'setup_complete': True,
                'backup_codes': backup_codes,
                'forced': forced,
            })
        # On invalid POST, fall through with the existing secret so the user
        # can re-attempt without scanning a new QR code.
    else:
        from .two_factor import generate_secret
        secret = generate_secret()
        request.session[TWO_FACTOR_SETUP_SECRET_KEY] = secret
        form = TwoFactorSetupForm(user=user, secret=secret)

    from .two_factor import otpauth_url
    return render(request, 'users/two_factor_setup.html', {
        'form': form,
        'setup_complete': False,
        'secret': secret,
        'otpauth_url': otpauth_url(secret, user.username),
        'forced': forced,
    })


@login_required
@ratelimit(key='user', rate='5/m', method='POST', block=False)
def two_factor_disable(request):
    """Disable 2FA — requires a fresh code from the user as confirmation."""
    user = request.user
    if not user.has_two_factor:
        messages.info(request, '您尚未启用两步验证。')
        return redirect('edit_profile')

    if request.method == 'POST' and getattr(request, 'limited', False):
        messages.error(request, '尝试次数过多，请稍后再试。')
        form = TwoFactorDisableForm(user=user)
    elif request.method == 'POST':
        form = TwoFactorDisableForm(request.POST, user=user)
        if form.is_valid():
            user.two_factor_secret = ''
            user.two_factor_enabled = False
            user.two_factor_backup_codes = ''
            # Staff users who disable 2FA must re-set it up before they
            # can resume using destructive admin actions.
            if user.is_staff or user.is_superuser:
                user.two_factor_setup_required = True
            user.save(update_fields=[
                'two_factor_secret', 'two_factor_enabled',
                'two_factor_backup_codes', 'two_factor_setup_required',
            ])
            request.session.pop(TWO_FACTOR_VERIFIED_KEY, None)
            request.session.pop(STAFF_REAUTH_AT_KEY, None)
            request.session.pop(STAFF_REAUTH_TARGET_KEY, None)
            messages.success(request, '两步验证已停用。')
            return redirect('edit_profile')
    else:
        form = TwoFactorDisableForm(user=user)

    return render(request, 'users/two_factor_disable.html', {'form': form})


@login_required
@ratelimit(key='user', rate='5/m', method='POST', block=False)
def two_factor_regenerate_backup(request):
    """Regenerate backup codes — requires a fresh TOTP / backup code."""
    user = request.user
    if not user.has_two_factor:
        messages.info(request, '请先启用两步验证。')
        return redirect('two_factor_setup')

    if request.method == 'POST' and getattr(request, 'limited', False):
        messages.error(request, '尝试次数过多，请稍后再试。')
        return redirect('edit_profile')
    elif request.method == 'POST':
        form = TwoFactorDisableForm(request.POST, user=user)
        if form.is_valid():
            from .two_factor import generate_backup_codes, hash_backup_codes
            backup_codes = generate_backup_codes()
            user.two_factor_backup_codes = hash_backup_codes(backup_codes)
            user.save(update_fields=['two_factor_backup_codes'])
            return render(request, 'users/two_factor_setup.html', {
                'form': None,
                'setup_complete': True,
                'backup_codes': backup_codes,
                'regenerated': True,
            })
        # On invalid POST, fall through with errors.
    else:
        form = TwoFactorDisableForm(user=user)

    return render(request, 'users/two_factor_disable.html', {
        'form': form,
        'regenerate_mode': True,
    })


@ratelimit(key='ip', rate='10/m', method='POST', block=False)
def two_factor_reauth(request):
    """Staff "sudo mode" — re-verify 2FA before destructive admin actions.

    The flow is:
      1. A view that needs sudo mode calls ``require_staff_reauth(request)``
         which, if the session's ``staff_reauth_at`` is stale or missing,
         redirects here with ``?next=<original_path>``.
      2. The user submits a TOTP code; on success, ``staff_reauth_at`` is
         refreshed and they're redirected back to ``next`` (admin-only).
    """
    user = request.user
    if not user.is_authenticated:
        return redirect('login')
    if not user.is_staff:
        # Non-staff users should never reach here; show a 403-style message.
        return render(request, 'users/two_factor_reauth.html', {
            'form': None, 'not_allowed': True,
        }, status=403)

    # Staff without 2FA configured must set it up first.
    if not user.has_two_factor:
        return redirect(reverse('two_factor_setup') + '?forced=1')

    next_url = request.GET.get('next') or request.POST.get('next') or ''
    # ``next`` must point inside the admin so this endpoint cannot be abused
    # as an open redirect. Warn when we drop an off-site target instead of
    # failing silently — otherwise a rejected ``next`` looks like the reauth
    # "did nothing" and sent the user to the dashboard for no reason.
    if next_url and not next_url.startswith('/admin') \
            and not url_has_allowed_host_and_scheme(
                next_url,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
        messages.warning(
            request,
            '返回地址不在本站域内，已忽略该跳转参数并改至后台首页。'
        )
        next_url = ''
    if not next_url:
        next_url = reverse('admin:index')

    if request.method == 'POST' and getattr(request, 'limited', False):
        messages.error(request, '尝试次数过多，请稍后再试。')
        form = TwoFactorReauthForm(user=user)
    elif request.method == 'POST':
        form = TwoFactorReauthForm(request.POST, user=user)
        if form.is_valid():
            request.session[STAFF_REAUTH_AT_KEY] = int(time.time())
            # Bind the stamp to the operation that was pending: only requests
            # to this path pass ``staff_reauth_valid`` afterwards, so
            # verifying and then canceling leaves no blanket sudo grant.
            request.session[STAFF_REAUTH_TARGET_KEY] = next_url
            request.session.pop(STAFF_REAUTH_NEXT_KEY, None)
            # Last line of defense at the redirect point: never send the
            # browser to an off-site URL even if a later code path touched
            # ``next_url`` between the top-of-view validation and here.
            if next_url and not url_has_allowed_host_and_scheme(
                next_url,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                messages.warning(
                    request,
                    '返回地址不在本站域内，已取消跳转并返回后台首页。'
                )
                next_url = reverse('admin:index')
            return redirect(next_url)
    else:
        form = TwoFactorReauthForm(user=user)

    return render(request, 'users/two_factor_reauth.html', {
        'form': form,
        'next_url': next_url,
    })


# ---- Staff-reauth helpers (used by admin actions & devlog/admin.py) -------

def staff_reauth_valid(request) -> bool:
    """True when the current staff session has a recent 2FA reauth stamp."""
    if not request.user.is_authenticated or not request.user.is_staff:
        return False
    if not request.user.has_two_factor:
        return False
    stamped = request.session.get(STAFF_REAUTH_AT_KEY)
    if not stamped:
        return False
    try:
        age = int(time.time()) - int(stamped)
    except (TypeError, ValueError):
        return False
    if not (0 <= age <= _staff_reauth_ttl()):
        return False
    # The stamp authorizes only the operation it was verified for: compare
    # the request path against the target that was pending when the TOTP
    # challenge completed. Verifying then canceling out → any other (or
    # later) sensitive operation needs a fresh challenge.
    from urllib.parse import urlsplit
    target = request.session.get(STAFF_REAUTH_TARGET_KEY) or ''
    if not target:
        return False
    return request.path == urlsplit(target).path


def require_staff_reauth(request, redirect_name='two_factor_reauth'):
    """Return a redirect to the reauth view when sudo mode is stale, else None.

    Callers (admin actions, backup/restore views) check this BEFORE mutating
    state. The original path is preserved via ``next`` so the reauth view can
    send the user back automatically after a successful challenge.
    """
    if staff_reauth_valid(request):
        return None
    target = reverse(redirect_name)
    full = request.get_full_path()
    from urllib.parse import urlencode
    return redirect(f'{target}?{urlencode({"next": full})}')


def consume_staff_reauth(request) -> None:
    """Burn the single-use sudo stamp once a sensitive operation runs.

    Every destructive operation (database backup/restore, bulk privilege or
    punishment changes) must be preceded by an explicit TOTP challenge, so
    the stamp is invalidated immediately after the operation it authorized —
    the next sensitive action requires a fresh ``two_factor_reauth`` round.
    Call this right before the mutation, after all permission / reauth
    checks have passed.
    """
    try:
        request.session.pop(STAFF_REAUTH_AT_KEY, None)
        request.session.pop(STAFF_REAUTH_TARGET_KEY, None)
    except Exception:  # pragma: no cover - defensive
        pass
