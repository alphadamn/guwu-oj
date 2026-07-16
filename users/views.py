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
)

logger = logging.getLogger(__name__)


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

        record_login_attempt(self.request, success=True)
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = context.get('form')
        context['captcha_required'] = (
            bool(form) and 'captcha_id' in getattr(form, 'fields', {})
        )
        context['captcha_url'] = reverse('captcha_image')
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
    response = captcha_image_response(png)
    # Expose the challenge_id in a JS-readable header so the template
    # can populate a hidden input without a page reload.
    if challenge_id:
        response['X-Captcha-Id'] = challenge_id
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
            login(request, user)
            try:
                send_welcome_email(user.email, username=user.username)
            except Exception:
                logger.exception('Welcome email failed for %s', user.email)
            messages.success(request, '注册成功！欢迎加入。')
            return redirect('home')
    else:
        form = UserRegisterForm(request=request)
    # Pass ``email_verification_required`` to the template so it can
    # conditionally show / hide the "send verification code" UI, plus
    # the current captcha challenge info so the image can be rendered.
    context = {
        'form': form,
        'email_verification_required': email_verification_required(),
        'captcha_required': 'captcha_id' in form.fields,
        'captcha_url': reverse('captcha_image'),
        'captcha_id': get_current_challenge_id(request) or '',
    }
    return render(request, 'users/register.html', context)


def password_reset_request(request):
    """Step 1: enter email to receive a verification code."""
    if request.method == 'POST':
        form = PasswordResetRequestForm(request.POST)
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
        form = PasswordResetRequestForm()

    return render(request, 'users/password_reset_request.html', {'form': form})


def password_reset_confirm(request, email):
    """Step 2: enter code + new password."""
    email = email.strip().lower()

    if request.method == 'POST':
        post = request.POST.copy()
        post['email'] = email
        form = PasswordResetForm(data=post)
        if form.is_valid():
            user = form.save()
            try:
                send_password_reset_done_email(user.email, username=user.username)
            except Exception:
                logger.exception('Password-reset-done email failed for %s', user.email)
            messages.success(request, '密码已成功重置，请使用新密码登录。')
            return redirect('login')
    else:
        form = PasswordResetForm(initial={'email': email})

    return render(
        request,
        'users/password_reset_confirm.html',
        {'form': form, 'email': email},
    )


@login_required
def profile(request, username):
    user = get_object_or_404(User, username=username)
    return render(request, 'users/profile.html', {'user_profile': user})


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
    if not challenge_id or not answer:
        return JsonResponse({'ok': False, 'message': '请填写图形验证码。'}, status=400)
    proof = verify_avatar_captcha(request, challenge_id, answer)
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
