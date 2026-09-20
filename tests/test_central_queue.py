"""Tests for the central judge queue dispatch (target architecture, Phase 1).

Covers the broker URL parser, the self-describing task payload, the
``OJ_CENTRAL_QUEUE`` feature flag routing in ``enqueue_judge`` (including the
fallback to the legacy per-machine path), and the central-lane name check the
worker task uses to skip legacy busy-slot bookkeeping.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from oj_project.settings import _parse_redis_broker_url
from submissions.judge_queue import (
    _build_task_payload,
    _central_queue_name,
    enqueue_judge,
    is_central_queue_name,
)
from submissions.models import Submission


class ParseRedisBrokerUrlTests(TestCase):
    def test_rediss_url_with_credentials_and_tls_params(self):
        machine = _parse_redis_broker_url(
            'rediss://:%21pass%40word@10.0.0.8:6380/2'
            '?ssl_ca_certs=/etc/tls/ca.crt&ssl_certfile=/etc/tls/c.crt'
            '&ssl_keyfile=/etc/tls/c.key'
        )
        self.assertEqual(machine['host'], '10.0.0.8')
        self.assertEqual(machine['port'], 6380)
        self.assertEqual(machine['db'], 2)
        self.assertEqual(machine['password'], '!pass@word')
        self.assertTrue(machine['tls'])
        self.assertEqual(machine['ca_cert_path'], '/etc/tls/ca.crt')
        self.assertEqual(machine['client_cert_path'], '/etc/tls/c.crt')
        self.assertEqual(machine['client_key_path'], '/etc/tls/c.key')

    def test_plain_url_defaults(self):
        machine = _parse_redis_broker_url('redis://127.0.0.1')
        self.assertEqual(machine['host'], '127.0.0.1')
        self.assertEqual(machine['port'], 6379)
        self.assertEqual(machine['db'], 0)
        self.assertEqual(machine['password'], '')
        self.assertFalse(machine['tls'])
        self.assertNotIn('ca_cert_path', machine)

    def test_invalid_scheme_rejected(self):
        with self.assertRaises(ValueError):
            _parse_redis_broker_url('amqp://localhost')
        with self.assertRaises(ValueError):
            _parse_redis_broker_url('redis://')


class BuildTaskPayloadTests(TestCase):
    def _payload(self, submission):
        return _build_task_payload(submission)

    def test_normal_submission_payload_fields(self):
        user = get_user_model().objects.create_user(
            username='central-payload-user', password='safe-test-password',
        )
        from problems.models import Problem

        problem = Problem.objects.create(
            title='Central payload', description='',
            input_format='', output_format='',
            time_limit=1500, memory_limit=512, created_by=user,
        )
        submission = Submission.objects.create(
            problem=problem, user=user, language='Python', code='print(1)',
        )
        payload = self._payload(submission)
        self.assertEqual(payload['submission_id'], submission.id)
        self.assertEqual(payload['language'], 'Python')
        self.assertEqual(payload['source_code'], 'print(1)')
        self.assertEqual(payload['test_case_set_id'], f'problem:{problem.id}')
        self.assertEqual(payload['time_limit'], 1500)
        self.assertEqual(payload['memory_limit'], 512)
        # enqueued_at must parse back to a recent timestamp.
        from django.utils.dateparse import parse_datetime

        parsed = parse_datetime(payload['enqueued_at'])
        self.assertIsNotNone(parsed)
        self.assertLessEqual(
            abs((timezone.now() - parsed).total_seconds()), 60,
        )

    def test_contest_submission_test_case_set_id(self):
        submission = SimpleNamespace(
            id=42, language='C++', code='int main(){}',
            contest_problem_id=7, problem_id=None,
            effective_problem=SimpleNamespace(time_limit=1000, memory_limit=256),
        )
        payload = self._payload(submission)
        self.assertEqual(payload['test_case_set_id'], 'contest_problem:7')
        self.assertEqual(payload['time_limit'], 1000)
        self.assertEqual(payload['memory_limit'], 256)

    def test_orphan_submission_has_no_test_case_set(self):
        submission = SimpleNamespace(
            id=1, language='Python', code='x',
            contest_problem_id=None, problem_id=None,
            effective_problem=None,
        )
        payload = self._payload(submission)
        self.assertIsNone(payload['test_case_set_id'])
        self.assertIsNone(payload['time_limit'])
        self.assertIsNone(payload['memory_limit'])


class CentralQueueNameTests(TestCase):
    def test_lane_names_cover_priority_tiers(self):
        self.assertEqual(_central_queue_name('pro'), 'judge:queue-pro')
        self.assertEqual(_central_queue_name('plus'), 'judge:queue-plus')
        self.assertEqual(_central_queue_name('default'), 'judge:queue')
        self.assertEqual(_central_queue_name('ai'), 'judge:queue-ai')

    def test_is_central_queue_name_independent_of_flag(self):
        # Workers do not set OJ_CENTRAL_QUEUE, so the check must rely on the
        # unambiguous queue name alone, flag on or off.
        self.assertTrue(is_central_queue_name('judge:queue'))
        self.assertTrue(is_central_queue_name('judge:queue-pro'))
        self.assertFalse(is_central_queue_name('judge:queue-pro-max'))
        self.assertFalse(is_central_queue_name('judge-1-pro'))
        self.assertFalse(is_central_queue_name(None))


CENTRAL_RQ_QUEUES = {
    'judge:queue-pro': {}, 'judge:queue-plus': {}, 'judge:queue': {},
    'judge:queue-ai': {}, 'default': {},
}


class EnqueueJudgeRoutingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='central-route-user', password='safe-test-password',
        )
        from problems.models import Problem

        self.problem = Problem.objects.create(
            title='Central routing', description='',
            input_format='', output_format='',
            time_limit=1000, memory_limit=256, created_by=self.user,
        )
        self.submission = Submission.objects.create(
            problem=self.problem, user=self.user, language='Python', code='print(1)',
        )

    @override_settings(
        OJ_CENTRAL_QUEUE=True,
        OJ_CENTRAL_QUEUE_NAME='judge:queue',
        RQ_QUEUES=CENTRAL_RQ_QUEUES,
    )
    def test_central_mode_enqueues_to_central_lane_with_payload(self):
        with patch('submissions.judge_queue.get_queue') as get_queue_mock:
            queue_mock = get_queue_mock.return_value
            queue_mock.name = 'judge:queue'
            job = enqueue_judge(self.submission.id)

        get_queue_mock.assert_called_once_with('judge:queue')
        args, kwargs = queue_mock.enqueue.call_args
        self.assertEqual(args[0].__name__, 'judge_submission_task')
        self.assertEqual(args[1], self.submission.id)
        meta = kwargs['meta']
        self.assertEqual(meta['dispatch_mode'], 'central')
        self.assertEqual(meta['submission_id'], self.submission.id)
        self.assertEqual(meta['payload']['submission_id'], self.submission.id)
        self.assertEqual(
            meta['payload']['test_case_set_id'], f'problem:{self.problem.id}',
        )
        self.assertEqual(job, queue_mock.enqueue.return_value)

    @override_settings(
        OJ_CENTRAL_QUEUE=True,
        OJ_CENTRAL_QUEUE_NAME='judge:queue',
        RQ_QUEUES=CENTRAL_RQ_QUEUES,
        OJ_MULTI_JUDGE_ENABLED=False,
    )
    def test_central_enqueue_failure_falls_back_to_legacy(self):
        legacy_queue = MagicMock()
        legacy_queue.name = 'low'
        with patch(
            'submissions.judge_queue.get_queue',
            side_effect=[ConnectionError('broker down'), legacy_queue],
        ) as get_queue_mock:
            job = enqueue_judge(self.submission.id)

        self.assertEqual(
            [call.args[0] for call in get_queue_mock.call_args_list],
            ['judge:queue', 'low'],
        )
        legacy_args, legacy_kwargs = legacy_queue.enqueue.call_args
        self.assertEqual(legacy_args[1], self.submission.id)
        self.assertNotIn('meta', legacy_kwargs)
        self.assertEqual(job, legacy_queue.enqueue.return_value)

    def test_flag_off_uses_legacy_path_only(self):
        with override_settings(
            OJ_CENTRAL_QUEUE=False, OJ_MULTI_JUDGE_ENABLED=False,
            RQ_QUEUES={'default': {}},
        ), patch('submissions.judge_queue.get_queue') as get_queue_mock:
            queue_mock = get_queue_mock.return_value
            enqueue_judge(self.submission.id)

        get_queue_mock.assert_called_once_with('low')

    @override_settings(OJ_CENTRAL_QUEUE=True, RQ_QUEUES={'default': {}})
    def test_unregistered_central_lane_falls_back(self):
        # Flag on but the central lane missing from RQ_QUEUES (e.g. DEMO_MODE
        # leftovers) must degrade to the legacy path, not raise.
        with patch('submissions.judge_queue.get_queue') as get_queue_mock:
            queue_mock = get_queue_mock.return_value
            with override_settings(OJ_MULTI_JUDGE_ENABLED=False):
                enqueue_judge(self.submission.id)
        get_queue_mock.assert_called_once_with('low')
