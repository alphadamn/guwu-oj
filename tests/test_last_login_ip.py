from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from users.models import User


class LastLoginIpTests(TestCase):
    def setUp(self):
        cache.clear()
        self.password = 'test-password'
        self.user = User.objects.create_user(
            username='login-ip-user',
            email='login-ip-user@example.com',
            password=self.password,
        )

    def login(self, **extra):
        return self.client.post(
            reverse('login'),
            {'username': self.user.username, 'password': self.password},
            **extra,
        )

    def test_successful_login_records_remote_address(self):
        response = self.login(REMOTE_ADDR='192.0.2.10')

        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.last_login_ip, '192.0.2.10')

    def test_successful_login_prefers_valid_forwarded_address(self):
        response = self.login(
            REMOTE_ADDR='192.0.2.10',
            HTTP_X_FORWARDED_FOR='2001:db8::1, 192.0.2.254',
        )

        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.last_login_ip, '2001:db8::1')

    def test_failed_login_does_not_change_last_login_ip(self):
        self.user.last_login_ip = '192.0.2.99'
        self.user.save(update_fields=['last_login_ip'])

        response = self.client.post(
            reverse('login'),
            {'username': self.user.username, 'password': 'wrong-password'},
            REMOTE_ADDR='192.0.2.10',
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.last_login_ip, '192.0.2.99')
