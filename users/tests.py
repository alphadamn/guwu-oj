from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from devlog.models import CaptchaConfig, RegistrationConfig
from users.email_utils import (
    issue_password_reset_code,
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

        self.assertContains(response, "fetch(captchaUrl")
        self.assertContains(response, "refreshCaptcha();")
        self.assertNotContains(response, 'src="/users/captcha/image/"')

    def test_invalid_captcha_keeps_email_verification_code_usable(self):
        response = self.client.post(reverse('register'), {
            **self.payload,
            'captcha_id': 'expired-challenge',
            'captcha_answer': 'wrong',
        })

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email=self.email).exists())
        self.assertTrue(verification_code_matches(self.email, self.code))

    def test_successful_registration_consumes_email_verification_code(self):
        captcha_config = CaptchaConfig.objects.get(pk=1)
        captcha_config.captcha_on_register = False
        captcha_config.save(update_fields=['captcha_on_register'])

        response = self.client.post(reverse('register'), self.payload)

        self.assertRedirects(response, reverse('home'))
        self.assertTrue(User.objects.filter(email=self.email).exists())
        self.assertFalse(verification_code_matches(self.email, self.code))


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

    def test_valid_reset_succeeds_and_clears_invalid_attempt_counter(self):
        code = issue_password_reset_code(self.email)
        attempt_key = PasswordResetForm._invalid_code_attempt_key(self.email)
        cache.set(attempt_key, 2, timeout=300)

        response = self.client.post(self.url, self._payload(code))

        self.assertRedirects(response, reverse('login'))
        self.assertIsNone(cache.get(attempt_key))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('ReplacementPassword123!'))

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
        self.assertEqual(cache.get(attempt_key), PasswordResetForm.MAX_INVALID_CODE_ATTEMPTS)

    def test_confirm_endpoint_is_limited_per_ip(self):
        for _ in range(5):
            response = self.client.post(self.url, self._payload('000000'), REMOTE_ADDR='203.0.113.9')
            self.assertEqual(response.status_code, 200)

        response = self.client.post(self.url, self._payload('000000'), REMOTE_ADDR='203.0.113.9')

        self.assertContains(response, '密码重置尝试过于频繁，请稍后再试。')
