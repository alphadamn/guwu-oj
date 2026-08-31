import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase


class EnvGeneratorAdminTests(TestCase):
    def test_anonymous_user_is_redirected(self):
        response = Client().get('/admin/env-generator/')

        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response['Location'])

    def test_staff_user_can_view_generator_without_post_form(self):
        User = get_user_model()
        user = User.objects.create_superuser(
            username='env-generator-admin',
            email='admin@example.com',
            password='safe-test-password',
        )
        # ``StaffTwoFactorMiddleware`` intercepts admin access and redirects
        # staff without 2FA to the setup page. This suite is not about 2FA
        # enforcement, so grant the test user a dummy TOTP credential so the
        # admin route runs normally.
        from users.two_factor import encrypt_secret, generate_secret
        user.two_factor_enabled = True
        user.two_factor_secret = encrypt_secret(generate_secret())
        user.save(update_fields=['two_factor_enabled', 'two_factor_secret'])
        client = Client()
        self.assertTrue(client.login(
            username=user.username,
            password='safe-test-password',
        ))

        response = client.get('/admin/env-generator/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '.env 配置生成器')
        self.assertNotContains(response, '<form method="post"')


class JudgeMachineSettingsTests(TestCase):
    def test_json_configuration_is_loaded_and_normalized(self):
        from oj_project.settings import _parse_judge_machines

        payload = json.dumps([{
            'name': 'judge-2',
            'host': 'judge.example.internal',
            'port': '6380',
            'db': '2',
            'queue': 'judge-2',
            'enabled': True,
            'weight': '3',
        }])
        result = _parse_judge_machines(payload, [{'name': 'fallback'}])

        self.assertEqual(result, [{
            'name': 'judge-2',
            'host': 'judge.example.internal',
            'port': 6380,
            'db': 2,
            'queue': 'judge-2',
            'enabled': True,
            'weight': 3,
            'tls': False,
            'password': '',
            'ca_cert_path': '',
            'client_cert_path': '',
            'client_key_path': '',
        }])

    def test_json_tls_configuration_is_loaded_and_normalized(self):
        from oj_project.settings import _parse_judge_machines

        payload = json.dumps([{
            'name': 'judge-tls',
            'host': 'judge.example.internal',
            'queue': 'judge-tls',
            'tls': True,
            'password': 'machine-password',
            'ca_cert_path': '/etc/guwu-oj/tls/ca.crt',
            'client_cert_path': '/etc/guwu-oj/tls/judge.crt',
            'client_key_path': '/etc/guwu-oj/tls/judge.key',
        }])

        result = _parse_judge_machines(payload, [])

        self.assertTrue(result[0]['tls'])
        self.assertEqual(result[0]['password'], 'machine-password')
        self.assertEqual(result[0]['ca_cert_path'], '/etc/guwu-oj/tls/ca.crt')
        self.assertEqual(result[0]['client_cert_path'], '/etc/guwu-oj/tls/judge.crt')
        self.assertEqual(result[0]['client_key_path'], '/etc/guwu-oj/tls/judge.key')

    def test_tls_configuration_requires_ca_and_complete_client_credentials(self):
        from oj_project.settings import _parse_judge_machines

        incomplete_tls = json.dumps([{
            'name': 'judge-tls', 'host': 'judge.internal', 'queue': 'judge-tls',
            'tls': True,
        }])
        incomplete_client_cert = json.dumps([{
            'name': 'judge-tls', 'host': 'judge.internal', 'queue': 'judge-tls',
            'ca_cert_path': '/tls/ca.crt', 'client_cert_path': '/tls/client.crt',
        }])

        with self.assertRaisesMessage(ValueError, 'TLS requires ca_cert_path'):
            _parse_judge_machines(incomplete_tls, [])
        with self.assertRaisesMessage(ValueError, 'requires both client_cert_path'):
            _parse_judge_machines(incomplete_client_cert, [])

    def test_queue_configuration_keeps_machine_tls_credentials_separate(self):
        from oj_project.settings import _rq_queue_entry

        queue = _rq_queue_entry({
            'host': 'judge.internal', 'port': 6380, 'db': 2,
            'tls': True, 'password': 'per-machine-password',
            'ca_cert_path': '/tls/ca.crt',
            'client_cert_path': '/tls/judge.crt',
            'client_key_path': '/tls/judge.key',
        })

        self.assertEqual(queue['PASSWORD'], 'per-machine-password')
        self.assertTrue(queue['SSL'])
        self.assertEqual(queue['REDIS_CLIENT_KWARGS']['ssl_ca_certs'], '/tls/ca.crt')
        self.assertEqual(queue['REDIS_CLIENT_KWARGS']['ssl_certfile'], '/tls/judge.crt')
        self.assertEqual(queue['REDIS_CLIENT_KWARGS']['ssl_keyfile'], '/tls/judge.key')

    def test_empty_json_configuration_uses_legacy_fallback(self):
        from oj_project.settings import _parse_judge_machines

        fallback = [{'name': 'judge-1'}]
        self.assertIs(_parse_judge_machines('', fallback), fallback)


class JudgeMachineCredentialTests(TestCase):
    def test_admin_stored_password_is_encrypted_and_recoverable(self):
        from submissions.models import JudgeMachine

        machine = JudgeMachine(name='judge-secure', queue='judge-secure')
        machine.set_redis_password('unique-machine-password')

        self.assertNotEqual(machine.redis_password_encrypted, 'unique-machine-password')
        self.assertEqual(machine.get_redis_password(), 'unique-machine-password')
