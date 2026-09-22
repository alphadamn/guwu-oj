"""Tests for the direct judge-API firewall sync and worker IP reporting.

Covers:
* public-IPv4 filtering of reported sources,
* state persistence / previous-IP reporting,
* iptables chain rebuild (default-deny, static + reported sources),
* ``POST /internal/judge/report_ip/`` auth, validation and anti-spoofing
  (the source address comes from the connection, never the body).
"""

import json
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from submissions import internal_views, judge_firewall

TEST_TOKEN = 'test-token-firewall'
PUBLIC_IP = '93.184.216.34'      # example.com — a genuinely public address
STATIC_IP = '64.90.3.112'


class _StateDirMixin:
    """Point the firewall state at a throwaway file for each test."""

    def setUp(self):
        self.state_path = os.path.join(
            tempfile.mkdtemp(), 'judge-direct-ips.json',
        )
        override = override_settings(
            OJ_JUDGE_DIRECT_STATE=self.state_path,
            OJ_JUDGE_DIRECT_STATIC_IPS=[STATIC_IP],
            OJ_JUDGE_DIRECT_CHAIN='TESTDIRECT',
        )
        override.enable()
        self.addCleanup(override.disable)


class FirewallStateTests(_StateDirMixin, TestCase):
    def test_is_usable_source_filters_non_public(self):
        for bad in ['', 'not-an-ip', '127.0.0.1', '10.1.2.3', '192.168.1.9',
                    '169.254.10.1', '203.0.113.7', '224.0.0.1', '0.0.0.0',
                    '::1', '2001:db8::1']:
            with self.subTest(ip=bad):
                self.assertFalse(judge_firewall.is_usable_source(bad))
        self.assertTrue(judge_firewall.is_usable_source(PUBLIC_IP))

    def test_record_worker_ip_keeps_previous_and_persists(self):
        self.assertIsNone(judge_firewall.record_worker_ip('judge-2', PUBLIC_IP))
        self.assertEqual(
            judge_firewall.record_worker_ip('judge-2', '8.8.8.8'), PUBLIC_IP,
        )
        self.assertEqual(
            judge_firewall.load_state()['judge-2']['ip'], '8.8.8.8',
        )
        with open(self.state_path, encoding='utf-8') as fh:
            self.assertEqual(json.load(fh)['judge-2']['ip'], '8.8.8.8')

    def test_load_state_tolerates_missing_or_broken_files(self):
        self.assertEqual(judge_firewall.load_state(), {})
        with open(self.state_path, 'w', encoding='utf-8') as fh:
            fh.write('{not json')
        self.assertEqual(judge_firewall.load_state(), {})

    def test_allowed_ips_puts_static_first_and_dedupes(self):
        judge_firewall.record_worker_ip('judge-2', PUBLIC_IP)
        judge_firewall.record_worker_ip('judge-1', STATIC_IP)
        judge_firewall.record_worker_ip('judge-3', '10.0.0.7')  # unusable
        self.assertEqual(judge_firewall.allowed_ips(), [STATIC_IP, PUBLIC_IP])


class ApplyFirewallTests(_StateDirMixin, TestCase):
    @staticmethod
    def _fake_run(args, **kwargs):
        # ``-C`` (rule exists?) must fail so the jump rule gets inserted.
        return SimpleNamespace(
            returncode=1 if '-C' in args else 0, stderr='', stdout='',
        )

    def _apply(self):
        with patch('submissions.judge_firewall.IPTABLES', '/bin/true'), \
             patch('submissions.judge_firewall.subprocess.run') as run:
            run.side_effect = self._fake_run
            sources = judge_firewall.apply_firewall()
        return sources, [call.args[0] for call in run.call_args_list]

    def test_chain_is_default_deny_with_all_allowed_sources(self):
        judge_firewall.record_worker_ip('judge-2', PUBLIC_IP)
        sources, calls = self._apply()

        self.assertEqual(sources, [STATIC_IP, PUBLIC_IP])
        self.assertIn(['/bin/true', '-N', 'TESTDIRECT'], calls)
        self.assertIn([
            '/bin/true', '-I', 'INPUT', '1', '-p', 'tcp', '--dport', '8446',
            '-j', 'TESTDIRECT',
        ], calls)
        self.assertIn(
            ['/bin/true', '-A', 'TESTDIRECT', '-s', STATIC_IP, '-j', 'ACCEPT'],
            calls,
        )
        self.assertIn(
            ['/bin/true', '-A', 'TESTDIRECT', '-s', PUBLIC_IP, '-j', 'ACCEPT'],
            calls,
        )
        self.assertEqual(
            calls[-1], ['/bin/true', '-A', 'TESTDIRECT', '-j', 'DROP'],
        )

    def test_empty_state_still_drops_everything(self):
        with override_settings(OJ_JUDGE_DIRECT_STATIC_IPS=[]):
            sources, calls = self._apply()
        self.assertEqual(sources, [])
        self.assertEqual(
            calls[-1], ['/bin/true', '-A', 'TESTDIRECT', '-j', 'DROP'],
        )
        self.assertFalse([call for call in calls if 'ACCEPT' in call])

    def test_existing_jump_rule_is_not_duplicated(self):
        with patch('submissions.judge_firewall.IPTABLES', '/bin/true'), \
             patch('submissions.judge_firewall.subprocess.run') as run:
            run.return_value = SimpleNamespace(
                returncode=0, stderr='', stdout='',
            )
            judge_firewall.apply_firewall()
        calls = [call.args[0] for call in run.call_args_list]
        self.assertFalse([call for call in calls if '-I' in call])

    def test_missing_iptables_is_a_noop(self):
        with patch(
            'submissions.judge_firewall.IPTABLES', '/nonexistent/iptables',
        ):
            self.assertIsNone(judge_firewall.apply_firewall())


@override_settings(JUDGE_INTERNAL_TOKEN=TEST_TOKEN)
class ReportIpViewTests(_StateDirMixin, TestCase):
    def _post(self, payload, **extra):
        return self.client.post(
            '/internal/judge/report_ip/', data=json.dumps(payload),
            content_type='application/json', **extra,
        )

    def test_requires_token(self):
        self.assertEqual(self._post({'worker_id': 'judge-2'}).status_code, 403)

    def test_worker_id_required(self):
        self.assertEqual(
            self._post({}, HTTP_X_JUDGE_TOKEN=TEST_TOKEN).status_code, 400,
        )

    def test_non_public_source_is_rejected(self):
        # Test client requests come from 127.0.0.1 — never whitelistable.
        resp = self._post({'worker_id': 'judge-2'}, HTTP_X_JUDGE_TOKEN=TEST_TOKEN)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['observed_ip'], '127.0.0.1')

    def test_records_edge_ip_and_ignores_body_supplied_ip(self):
        with patch.object(
            internal_views, 'apply_firewall', return_value=[PUBLIC_IP],
        ) as apply_mock:
            resp = self._post(
                {'worker_id': 'judge-2', 'ip': '8.8.8.8'},
                HTTP_X_JUDGE_TOKEN=TEST_TOKEN,
                HTTP_X_FORWARDED_FOR=f'{PUBLIC_IP}, 10.0.0.1',
            )

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['ip'], PUBLIC_IP)
        self.assertIsNone(data['previous_ip'])
        self.assertTrue(data['applied'])
        apply_mock.assert_called_once_with()
        self.assertEqual(
            judge_firewall.load_state()['judge-2']['ip'], PUBLIC_IP,
        )

    def test_renumbering_reports_previous_ip(self):
        def report(ip):
            return self._post(
                {'worker_id': 'judge-2'}, HTTP_X_JUDGE_TOKEN=TEST_TOKEN,
                HTTP_X_FORWARDED_FOR=ip,
            )

        with patch.object(internal_views, 'apply_firewall', return_value=[]):
            self.assertIsNone(report(PUBLIC_IP).json()['previous_ip'])
            data = report('8.8.4.4').json()

        self.assertEqual(data['ip'], '8.8.4.4')
        self.assertEqual(data['previous_ip'], PUBLIC_IP)
        self.assertEqual(
            judge_firewall.allowed_ips(), [STATIC_IP, '8.8.4.4'],
        )
