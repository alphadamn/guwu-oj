from django.test import TestCase
from django.urls import reverse

from devlog.models import CaptchaConfig, RegistrationConfig
from users.email_utils import issue_verification_code, verification_code_matches
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
