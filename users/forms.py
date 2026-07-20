from django import forms
from django.contrib.auth.forms import UserCreationForm, SetPasswordForm, AuthenticationForm
from django.core.exceptions import ValidationError
from PIL import Image

from .models import User, AvatarBlob
from .email_utils import (
    check_verification_code,
    check_password_reset_code,
)
from .captcha import (
    check_challenge as _captcha_check,
    get_current_challenge_id as _captcha_current_id,
    login_requires_captcha as _login_captcha_required,
    CAPTCHA_ON_REGISTER as _captcha_on_register_cfg,
)


def _captcha_on_register() -> bool:
    try:
        fn = _captcha_on_register_cfg
        if callable(fn):
            return bool(fn())
        return bool(fn)
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Captcha mixin — adds a ``captcha_answer`` + hidden ``captcha_id`` field
# conditionally, and performs verification in ``clean``. Forms that mix
# it in are responsible for deciding *when* the captcha is required
# (registration = always, login = only after 1 failed attempt).
# ---------------------------------------------------------------------------

class CaptchaMixin(forms.Form):
    """Drop-in captcha support. Subclasses enable it by calling
    :meth:`_enable_captcha` from ``__init__``. Otherwise the fields
    are simply absent.
    """

    def _enable_captcha(self):
        self.fields['captcha_id'] = forms.CharField(
            required=True,
            widget=forms.HiddenInput(),
        )
        self.fields['captcha_answer'] = forms.CharField(
            label='图形验证码',
            max_length=12,
            required=True,
            widget=forms.TextInput(attrs={
                'autocomplete': 'off',
                'placeholder': '请输入图形验证码',
            }),
        )

    def clean_captcha(self):
        """Call from the subclass's clean chain."""
        if 'captcha_id' not in self.fields:
            return None
        captcha_id = self.cleaned_data.get('captcha_id') or ''
        captcha_answer = self.cleaned_data.get('captcha_answer') or ''
        # ``request`` is stashed on the form by the view so we can
        # access the client IP + the session for per-IP rate-limits.
        request = getattr(self, 'request', None)
        if not captcha_id or not captcha_answer:
            raise ValidationError('请输入图形验证码。')
        if not _captcha_check(request, captcha_id, captcha_answer):
            raise ValidationError('图形验证码无效或已过期，请刷新后重试。')
        return captcha_answer


class UserRegisterForm(UserCreationForm, CaptchaMixin):
    email = forms.EmailField(required=True)
    nickname = forms.CharField(label='昵称（可选）', max_length=50, required=False)
    referral_code = forms.CharField(
        label='邀请码（可选）', max_length=16, required=False,
        help_text='通过邀请链接访问时会自动填写。',
    )
    # verification_code is OPTIONAL at class declaration time — we add/remove
    # it dynamically in ``__init__`` based on
    # ``RegistrationConfig.email_verification_required`` so the admin panel
    # can toggle the feature without a redeploy.
    password1 = forms.CharField(
        label='密码',
        strip=False,
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
    )
    password2 = forms.CharField(
        label='确认密码',
        strip=False,
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
    )

    class Meta:
        model = User
        fields = ('username', 'email', 'nickname', 'password1', 'password2')

    def __init__(self, *args, request=None, **kwargs):
        # Stash the request so the captcha mixin can do per-IP rate
        # limiting — matches the pattern used by django.contrib.auth.
        self.request = request
        super().__init__(*args, **kwargs)
        # Add / remove the verification_code field based on config.
        # This keeps the rest of the form clean — ``clean_verification_code``
        # short-circuits when the field isn't declared, and ``save`` never
        # needs it.
        if self._email_verification_required():
            self.fields['verification_code'] = forms.CharField(
                label='邮箱验证码',
                max_length=self._code_length(),
                min_length=self._code_length(),
                required=True,
                widget=forms.TextInput(attrs={
                    'autocomplete': 'one-time-code',
                    'inputmode': 'numeric',
                }),
            )
        else:
            # Make sure a stale browser POST with ``verification_code`` is
            # silently dropped rather than failing field validation.
            self.fields.pop('verification_code', None)
        # Optional: graphical captcha — honours the admin config flag.
        if _captcha_on_register():
            self._enable_captcha()

    # ----- Config helpers (graceful if migrations / DB are down) -----
    @staticmethod
    def _registration_config():
        try:
            from devlog.models import RegistrationConfig
            return RegistrationConfig.objects.filter(pk=1).first()
        except Exception:
            return None

    def _email_verification_required(self) -> bool:
        cfg = self._registration_config()
        if cfg is None:
            return True  # safe default: verification required
        return bool(getattr(cfg, 'email_verification_required', True))

    def _code_length(self) -> int:
        cfg = self._registration_config()
        try:
            val = int(getattr(cfg, 'verification_code_length', 6))
            if val < 4:
                val = 6
            return val
        except (TypeError, ValueError):
            return 6

    # ----- Standard clean hooks -----
    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError('该邮箱已被注册。')
        return email

    def clean_referral_code(self):
        referral_code = self.cleaned_data['referral_code'].strip()
        if referral_code and not User.objects.filter(referral_code=referral_code).exists():
            raise ValidationError('邀请码无效。')
        return referral_code

    def clean_verification_code(self):
        # method won't even be called. But we still short-circuit defensively.
        if not self._email_verification_required():
            return ''
        email = self.cleaned_data.get('email') or self.data.get('email', '')
        code = self.cleaned_data['verification_code'].strip()
        if not email:
            raise ValidationError('请先填写邮箱。')
        if not check_verification_code(email, code):
            raise ValidationError('验证码无效或已过期。')
        return code

    def clean(self):
        cleaned_data = super().clean()
        self.clean_captcha()
        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data['email']
        user.nickname = self.cleaned_data.get('nickname', '')
        referral_code = self.cleaned_data.get('referral_code', '').strip()
        if referral_code:
            user.referrer = User.objects.filter(referral_code=referral_code).first()
        if commit:
            user.save()
        return user


class LoginFormWithCaptcha(AuthenticationForm, CaptchaMixin):
    """Standard Django ``AuthenticationForm`` plus a captcha that is
    *conditionally* required based on per-IP failed-login count.

    The view flips ``captcha_required`` before instantiating the form.
    """

    def __init__(self, request=None, *args, captcha_required=False, **kwargs):
        # ``AuthenticationForm`` treats the FIRST positional argument as
        # ``request``; by matching that positional contract, we avoid
        # "multiple values for argument 'request'" when the view passes
        # ``request`` via ``get_form_kwargs()``.
        self.request = request
        self.captcha_required = captcha_required
        super().__init__(request, *args, **kwargs)
        if captcha_required or (request is not None and _login_captcha_required(request)):
            self._enable_captcha()

    def clean(self):
        cleaned_data = super().clean()
        self.clean_captcha()
        return cleaned_data


class SendVerificationCodeForm(forms.Form):
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={'placeholder': '请输入邮箱'}),
    )

    def clean_email(self):
        return self.cleaned_data['email'].strip().lower()


class PasswordResetRequestForm(forms.Form):
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={'placeholder': '请输入注册时使用的邮箱'}),
    )

    def clean_email(self):
        return self.cleaned_data['email'].strip().lower()


class PasswordResetForm(SetPasswordForm):
    """Second step of password reset: verify code and set a new password."""

    email = forms.EmailField(
        required=True,
        widget=forms.HiddenInput(),
    )
    verification_code = forms.CharField(
        label='邮箱验证码',
        max_length=6,
        min_length=6,
        required=True,
        widget=forms.TextInput(attrs={
            'autocomplete': 'one-time-code',
            'inputmode': 'numeric',
        }),
    )

    def __init__(self, user=None, *args, **kwargs):
        # We override to make the user argument optional from the form side;
        # the view will provide a user when the form is ready to save.
        if user is None:
            # Build a throwaway user so SetPasswordForm.__init__ works;
            # we override clean to look up the real user by email.
            class _DummyUser:
                pk = None
                def set_password(self, raw_password):
                    pass
            user = _DummyUser()
        super().__init__(user, *args, **kwargs)

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        try:
            user = User.objects.get(email__iexact=email, is_active=True)
        except User.DoesNotExist:
            raise ValidationError('该邮箱未注册。')
        self.cleaned_data['_user'] = user
        return email

    def clean_verification_code(self):
        email = self.cleaned_data.get('email') or self.data.get('email', '')
        code = self.cleaned_data['verification_code'].strip()
        if not check_password_reset_code(email, code):
            raise ValidationError('验证码无效或已过期。')
        return code

    def save(self, commit=True):
        user = self.cleaned_data.get('_user')
        if user is None:
            raise ValidationError('用户信息无效。')
        user.set_password(self.cleaned_data['new_password1'])
        if commit:
            user.save()
        return user


class UserUpdateForm(forms.ModelForm):
    MAX_AVATAR_SIZE = 5 * 1024 * 1024  # 5MB
    ALLOWED_CONTENT_TYPES = {'image/jpeg', 'image/png', 'image/webp', 'image/gif'}

    avatar = forms.FileField(
        required=False,
        widget=forms.ClearableFileInput(attrs={'accept': 'image/*'})
    )

    class Meta:
        model = User
        fields = ('nickname', 'bio')
        widgets = {
            'bio': forms.Textarea(attrs={'rows': 4}),
        }

    def clean_avatar(self):
        avatar = self.cleaned_data.get('avatar')
        if avatar is False:
            return False
        if not avatar:
            return None
        if avatar.size > self.MAX_AVATAR_SIZE:
            raise ValidationError('头像大小不能超过 5MB。')
        content_type = getattr(avatar, 'content_type', '')
        if content_type not in self.ALLOWED_CONTENT_TYPES:
            raise ValidationError('仅支持 JPG/PNG/WEBP/GIF 格式头像。')
        try:
            avatar.seek(0)
            img = Image.open(avatar)
            img.verify()
            avatar.seek(0)
        except Exception as exc:
            raise ValidationError('上传文件不是有效图片。') from exc
        return avatar

    def save(self, commit=True):
        from .models import AvatarBlob

        user = super().save(commit=False)
        avatar = self.cleaned_data.get('avatar')

        if avatar is False:
            AvatarBlob.objects.filter(user=user).delete()
        elif avatar:
            avatar.seek(0)
            data = avatar.read()
            content_type = getattr(avatar, 'content_type', 'image/jpeg')
            AvatarBlob.objects.update_or_create(
                user=user,
                defaults={'content_type': content_type, 'data': data},
            )

        if commit:
            user.save()
        return user
