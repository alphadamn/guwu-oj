import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase


class EnvGeneratorAdminTests(TestCase):
    def test_anonymous_user_is_redirected(self):
        response = Client().get('/admin/env-generator/')

        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response['Location'])

    def test_staff_user_can_view_generator_without_post_form(self):
        user = get_user_model().objects.create_superuser(
            username='env-generator-admin',
            email='admin@example.com',
            password='safe-test-password',
        )
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
        }])

    def test_empty_json_configuration_uses_legacy_fallback(self):
        from oj_project.settings import _parse_judge_machines

        fallback = [{'name': 'judge-1'}]
        self.assertIs(_parse_judge_machines('', fallback), fallback)
