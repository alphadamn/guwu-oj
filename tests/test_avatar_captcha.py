import base64

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from devlog.models import CaptchaConfig
from users.models import AvatarBlob, User


TINY_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk'
    '+A8AAQUBAScY42YAAAAASUVORK5CYII='
)


@override_settings(
    CACHES={
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'avatar-captcha-tests',
        }
    }
)
class AvatarCaptchaTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='avatar-owner',
            email='avatar-owner@example.com',
            password='test-password',
        )
        AvatarBlob.objects.create(
            user=self.user,
            content_type='image/png',
            data=TINY_PNG,
        )
        CaptchaConfig.objects.create(
            pk=1,
            captcha_avatar_captcha_enabled=True,
            captcha_avatar_request_limit=2,
            captcha_avatar_request_window_minutes=1,
        )
        self.client = Client(REMOTE_ADDR='192.0.2.10')
        self.assertTrue(self.client.login(
            username='avatar-owner',
            password='test-password',
        ))

    def avatar_url(self):
        return reverse('avatar', kwargs={'username': self.user.username})

    def test_requests_below_threshold_return_image(self):
        first = self.client.get(self.avatar_url())
        second = self.client.get(self.avatar_url())

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first['Content-Type'], 'image/png')

    def test_request_after_threshold_requires_captcha(self):
        self.client.get(self.avatar_url())
        self.client.get(self.avatar_url())

        response = self.client.get(self.avatar_url())

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response['X-Captcha-Required'], '1')
        self.assertJSONEqual(
            response.content,
            {
                'captcha_required': True,
                'message': '头像访问过于频繁，请完成图形验证码。',
            },
        )

    def test_disabled_protection_allows_requests(self):
        CaptchaConfig.objects.update(captcha_avatar_captcha_enabled=False)
        for _ in range(4):
            response = self.client.get(self.avatar_url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')

    def test_same_ip_shares_rate_across_sessions(self):
        self.client.get(self.avatar_url())
        other_client = Client(REMOTE_ADDR='192.0.2.10')
        self.assertTrue(other_client.login(
            username='avatar-owner',
            password='test-password',
        ))
        other_client.get(self.avatar_url())

        response = other_client.get(self.avatar_url())

        self.assertEqual(response.status_code, 429)

    def test_different_ips_have_independent_rates(self):
        self.client.get(self.avatar_url())
        other_client = Client(REMOTE_ADDR='192.0.2.11')
        self.assertTrue(other_client.login(
            username='avatar-owner',
            password='test-password',
        ))

        response = other_client.get(self.avatar_url())

        self.assertEqual(response.status_code, 200)

    def test_captcha_proof_can_be_reused_until_it_expires(self):
        from unittest.mock import patch

        self.client.get(self.avatar_url())
        self.client.get(self.avatar_url())
        with patch('users.captcha.check_challenge', return_value=True):
            response = self.client.post(
                reverse('verify_avatar_captcha'),
                {'captcha_id': 'challenge', 'captcha_answer': 'answer'},
            )

        self.assertEqual(response.status_code, 200)
        proof = response.json()['proof']
        self.assertEqual(
            self.client.get(
                self.avatar_url(),
                HTTP_X_AVATAR_CAPTCHA_PROOF=proof,
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                self.avatar_url(),
                HTTP_X_AVATAR_CAPTCHA_PROOF=proof,
            ).status_code,
            200,
        )
        self.assertEqual(self.client.get(self.avatar_url()).status_code, 429)
