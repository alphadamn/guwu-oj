from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from devlog.models import CaptchaConfig, RegistrationConfig
from users.email_utils import (
    issue_verification_code,
    verification_code_matches,
)
from users.forms import PasswordResetForm
from users.models import User


class RegistrationVerificationCodeTests(TestCase):
    def setUp(self):
        registration_config, _ = RegistrationConfig.objects.get_or_create(pk=1)
        registration_config.email_verification_required = True
        registration_config.save(update_fields=['email_verification_required'])

        captcha_config, _ = CaptchaConfig.objects.get_or_create(pk=1)
        captcha_config.captcha_on_register = True
        captcha_config.save(update_fields=['captcha_on_register'])

        self.email = 'new-user@example.com'
        self.code = issue_verification_code(self.email)
        self.payload = {
            'username': 'new-user',
            'email': self.email,
            'nickname': '',
            'referral_code': '',
            'password1': 'SafePassword123!',
            'password2': 'SafePassword123!',
            'verification_code': self.code,
        }

    def test_registration_page_uses_header_aware_captcha_loading(self):
        response = self.client.get(reverse('register'))

        # The image is fetched from JS so the X-Captcha-Id response header can
        # be captured; the widget handles that, so the page must not point an
        # <img src> straight at the captcha endpoint.
        self.assertContains(response, 'js/captcha-widget.js')
        self.assertContains(response, 'data-captcha-url="/users/captcha/image/"')
        self.assertNotContains(response, 'src="/users/captcha/image/"')

        widget = finders.find('js/captcha-widget.js')
        self.assertIsNotNone(widget)
        with open(widget, encoding='utf-8') as fh:
            self.assertIn('X-Captcha-Id', fh.read())

    def test_invalid_captcha_keeps_email_verification_code_usable(self):
        response = self.client.post(reverse('register'), {
            **self.payload,
            'captcha_id': 'expired-challenge',
            'captcha_answer': 'wrong',
        })

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email=self.email).exists())
        self.assertTrue(verification_code_matches(self.email, self.code))


class PasswordResetRateLimitTests(TestCase):
    def setUp(self):
        self.email = 'reset-user@example.com'
        self.password = 'OriginalPassword123!'
        self.user = User.objects.create_user(
            username='reset-user',
            email=self.email,
            password=self.password,
        )
        self.url = reverse('password_reset_confirm', kwargs={'email': self.email})
        cache.delete(PasswordResetForm._invalid_code_attempt_key(self.email))

    def _payload(self, code):
        return {
            'verification_code': code,
            'new_password1': 'ReplacementPassword123!',
            'new_password2': 'ReplacementPassword123!',
        }

    def test_invalid_code_attempts_are_limited_per_email(self):
        attempt_key = PasswordResetForm._invalid_code_attempt_key(self.email)
        for index in range(PasswordResetForm.MAX_INVALID_CODE_ATTEMPTS):
            response = self.client.post(
                self.url,
                self._payload('000000'),
                REMOTE_ADDR=f'203.0.113.{index + 1}',
            )
            self.assertFormError(response.context['form'], 'verification_code', '验证码无效或已过期。')

        response = self.client.post(
            self.url,
            self._payload('000000'),
            REMOTE_ADDR='203.0.113.99',
        )

        self.assertFormError(response.context['form'], 'verification_code', '验证码尝试次数过多，请 5 分钟后再试。')
        from users.sliding_window import sliding_count
        self.assertEqual(
            sliding_count(attempt_key, PasswordResetForm.INVALID_CODE_ATTEMPT_TTL),
            PasswordResetForm.MAX_INVALID_CODE_ATTEMPTS,
        )

    def test_confirm_endpoint_is_limited_per_ip(self):
        for _ in range(5):
            response = self.client.post(self.url, self._payload('000000'), REMOTE_ADDR='203.0.113.9')
            self.assertEqual(response.status_code, 200)

        response = self.client.post(self.url, self._payload('000000'), REMOTE_ADDR='203.0.113.9')

        self.assertContains(response, '密码重置尝试过于频繁，请稍后再试。')


class SlidingWindowRateLimitTests(TestCase):
    """The sliding window counts events in the *trailing* window rather
    than resetting on a clock boundary."""

    def test_allows_up_to_limit_then_denies(self):
        from users.sliding_window import sliding_allow, sliding_clear
        key = 'test:sliding:allow-deny'
        sliding_clear(key)
        try:
            for _ in range(3):
                self.assertTrue(sliding_allow(key, 3, 60))
            self.assertFalse(sliding_allow(key, 3, 60))
        finally:
            sliding_clear(key)

    def test_window_slides(self):
        import time
        from users.sliding_window import sliding_allow, sliding_clear
        key = 'test:sliding:slides'
        sliding_clear(key)
        try:
            # Fill a 1-second window.
            self.assertTrue(sliding_allow(key, 1, 1))
            self.assertFalse(sliding_allow(key, 1, 1))
            # After the window elapses the budget returns (a fixed bucket
            # keyed by wall-clock seconds would behave the same here; the
            # key assertion is that the *old event* has aged out).
            time.sleep(1.1)
            self.assertTrue(sliding_allow(key, 1, 1))
        finally:
            sliding_clear(key)

    def test_clear_forgets_history(self):
        from users.sliding_window import sliding_allow, sliding_clear, sliding_count
        key = 'test:sliding:clear'
        sliding_clear(key)
        try:
            sliding_allow(key, 2, 60)
            sliding_allow(key, 2, 60)
            self.assertEqual(sliding_count(key, 60), 2)
            sliding_clear(key)
            self.assertEqual(sliding_count(key, 60), 0)
            self.assertTrue(sliding_allow(key, 2, 60))
        finally:
            sliding_clear(key)
