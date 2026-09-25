"""Tests for the central judge queue dispatch (Celery).

Covers the broker URL parser, the self-describing task payload, the
Celery message-priority mapping (pro > plus > free > ai, lowest number
drains first on the Redis transport) and the ``enqueue_judge`` dispatch
path (mark_queued gate, on_commit dispatch, no-broker degradation).
"""

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from oj_project.settings import _parse_redis_broker_url
from submissions.judge_queue import (
    JUDGE_QUEUE,
    _build_task_payload,
    celery_priority,
    enqueue_judge,
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


class CeleryPriorityTests(TestCase):
    def test_tier_ordering_pro_first_ai_last(self):
        self.assertEqual(celery_priority('pro'), 0)
        self.assertEqual(celery_priority('plus'), 3)
        self.assertEqual(celery_priority('default'), 6)
        self.assertEqual(celery_priority('ai'), 9)
        # The Redis transport drains lower numbers first.
        self.assertLess(celery_priority('pro'), celery_priority('plus'))
        self.assertLess(celery_priority('plus'), celery_priority('default'))
        self.assertLess(celery_priority('default'), celery_priority('ai'))

    def test_unknown_tier_falls_back_to_free(self):
        self.assertEqual(celery_priority('nonsense'), 6)

    def test_free_user_resolves_to_default_tier(self):
        from submissions.judge_queue import (
            PRIORITY_DEFAULT,
            resolve_submission_priority,
        )

        user = get_user_model().objects.create_user(
            username='free-priority-user', password='safe-test-password',
        )
        submission = SimpleNamespace(user=user)
        self.assertEqual(resolve_submission_priority(submission), PRIORITY_DEFAULT)


class EnqueueJudgeDispatchTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='dispatch-user', password='safe-test-password',
        )
        from problems.models import Problem

        self.problem = Problem.objects.create(
            title='Dispatch', description='',
            input_format='', output_format='',
            time_limit=1000, memory_limit=256, created_by=self.user,
        )
        self.submission = Submission.objects.create(
            problem=self.problem, user=self.user, language='Python', code='print(1)',
        )

    def test_enqueues_with_queue_and_priority_after_commit(self):
        with patch('submissions.judge_queue.judge_submission_task') as task, \
             patch('submissions.judge_queue.resolve_submission_priority',
                   return_value='ai'):
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                result = enqueue_judge(self.submission.id)

        self.assertEqual(result, self.submission.id)
        self.assertEqual(len(callbacks), 1)
        task.apply_async.assert_called_once_with(
            args=[self.submission.id], queue=JUDGE_QUEUE, priority=9,
        )
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.judge_state, 'QUEUED')

    def test_free_tier_dispatch_uses_default_priority(self):
        with patch('submissions.judge_queue.judge_submission_task') as task:
            with self.captureOnCommitCallbacks(execute=True):
                enqueue_judge(self.submission.id)

        task.apply_async.assert_called_once_with(
            args=[self.submission.id], queue=JUDGE_QUEUE, priority=6,
        )

    def test_terminal_row_is_not_dispatched(self):
        from submissions.claiming import claim_submission, finalize_claim

        token = claim_submission(self.submission.id, 'worker-a')
        finalize_claim(self.submission.id, token, 'Accepted')

        with patch('submissions.judge_queue.judge_submission_task') as task:
            self.assertIsNone(enqueue_judge(self.submission.id))
            task.apply_async.assert_not_called()

    @override_settings(CELERY_BROKER_URL=None)
    def test_no_broker_configured_leaves_row_queued(self):
        # DEMO_MODE has no broker: the row is parked in QUEUED (the reaper
        # can recover it later) and nothing is dispatched.
        with patch('submissions.judge_queue.judge_submission_task') as task:
            self.assertIsNone(enqueue_judge(self.submission.id))
            task.apply_async.assert_not_called()
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.judge_state, 'QUEUED')
