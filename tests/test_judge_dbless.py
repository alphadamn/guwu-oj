"""Tests for Phase 3: DB-less workers, internal claim API, result queue.

Covers the pure judging core, the internal claim/heartbeat API, the
fencing-protected consumer writeback, reliable-queue helpers and the
DB-less task glue (HTTP claim -> judge core -> result envelope).
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, Client, override_settings

from problems.models import Problem, TestCase as ProblemTestCase
from submissions.models import Submission

TEST_TOKEN = 'phase3-internal-token'


def _make_problem(user, cases=2):
    problem = Problem.objects.create(
        title='DBless fixture', description='', input_format='',
        output_format='', time_limit=1000, memory_limit=256,
        created_by=user,
    )
    for _ in range(cases):
        ProblemTestCase.objects.create(
            problem=problem, input_data='', expected_output='',
        )
    return problem


class JudgeCoreTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='core-user', password='safe-test-password',
        )
        self.problem = _make_problem(self.user, cases=2)

    def _run_core(self, code='print("")', language='Python'):
        from submissions.judge_core import judge_spec
        spec = {
            'submission_id': 999001,
            'language': language,
            'code': code,
            'user_id': self.user.id,
            'time_limit_ms': 1000,
            'memory_limit_mb': 256,
            'cases': [
                {'index': 1, 'input': '', 'expected': ''},
                {'index': 2, 'input': '', 'expected': ''},
            ],
        }
        with patch('submissions.judge.JudgeContainer') as container_class, \
             patch('submissions.judge.container_pool.acquire',
                   return_value=None), \
             patch(
                 'submissions.judge.SandboxRunner._parse_time_stderr',
                 return_value=(3, 0)):
            container = container_class.return_value.__enter__.return_value
            container.exec.return_value = SimpleNamespace(
                returncode=0, stdout='\n', stderr='',
            )
            return judge_spec(spec, global_timeout_sec=5)

    def test_core_returns_accepted_outcome_without_db_writes(self):
        before = Submission.objects.count()
        outcome = self._run_core()
        self.assertEqual(outcome['verdict'], 'Accepted')
        self.assertEqual(len(outcome['cases']), 2)
        self.assertEqual(outcome['cases'][0]['index'], 1)
        self.assertEqual(outcome['cases'][0]['status'], 'Accepted')
        self.assertGreaterEqual(outcome['runtime_ms'], 0)
        # The core must never create rows.
        self.assertEqual(Submission.objects.count(), before)

    def test_core_compile_error_outcome(self):
        with patch('submissions.judge.JudgeContainer') as container_class, \
             patch('submissions.judge.container_pool.acquire',
                   return_value=None):
            container = container_class.return_value.__enter__.return_value
            container.exec.return_value = SimpleNamespace(
                returncode=1, stdout='', stderr='boom: error',
            )
            from submissions.judge_core import judge_spec
            spec = {
                'submission_id': 999002, 'language': 'C++',
                'code': 'int main( {', 'user_id': 1,
                'time_limit_ms': 1000, 'memory_limit_mb': 256,
                'cases': [{'index': 1, 'input': '', 'expected': ''}],
            }
            outcome = judge_spec(spec)
        self.assertEqual(outcome['verdict'], 'Compile Error')
        self.assertEqual(outcome['cases'][0]['status'], 'Skipped')
        self.assertIn('boom', outcome['cases'][0]['error_message'])


@override_settings(JUDGE_INTERNAL_TOKEN=TEST_TOKEN)
class InternalApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='api-user', password='safe-test-password',
        )
        self.problem = _make_problem(self.user, cases=3)
        self.submission = Submission.objects.create(
            problem=self.problem, user=self.user,
            language='Python', code='print(1)',
        )
        self.client = Client()

    def _post(self, url, payload):
        return self.client.post(
            url, data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_JUDGE_TOKEN=TEST_TOKEN,
        )

    def test_claim_requires_token(self):
        resp = self.client.post(
            '/internal/judge/claim/', data='{}',
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_claim_returns_bundle_and_second_loses(self):
        resp = self._post('/internal/judge/claim/', {
            'submission_id': self.submission.id, 'worker_id': 'w1',
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['claimable'])
        self.assertEqual(data['language'], 'Python')
        self.assertEqual(data['code'], 'print(1)')
        self.assertEqual(data['time_limit_ms'], 1000)
        self.assertEqual(len(data['cases']), 3)
        self.assertEqual(data['cases'][0],
                         {'index': 1, 'input': '', 'expected': ''})
        self.assertTrue(data['claim_token'])

        self.submission.refresh_from_db()
        self.assertEqual(self.submission.judge_state, 'JUDGING')
        self.assertEqual(self.submission.worker_id, 'w1')

        resp2 = self._post('/internal/judge/claim/', {
            'submission_id': self.submission.id, 'worker_id': 'w2',
        })
        self.assertEqual(resp2.status_code, 409)
        self.assertFalse(resp2.json()['claimable'])

    def test_heartbeat_alive_then_dead_after_requeue(self):
        data = self._post('/internal/judge/claim/', {
            'submission_id': self.submission.id, 'worker_id': 'w1',
        }).json()
        resp = self._post('/internal/judge/heartbeat/', {
            'submission_id': self.submission.id,
            'claim_token': data['claim_token'],
        })
        self.assertEqual(resp.json(), {'alive': True})

        from submissions.claiming import requeue_claim
        requeue_claim(self.submission.id)

        resp = self._post('/internal/judge/heartbeat/', {
            'submission_id': self.submission.id,
            'claim_token': data['claim_token'],
        })
        self.assertEqual(resp.json(), {'alive': False})


class ConsumerWritebackTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='consumer-user', password='safe-test-password',
        )
        self.problem = _make_problem(self.user, cases=2)
        self.submission = Submission.objects.create(
            problem=self.problem, user=self.user,
            language='Python', code='print(1)',
        )

    def _claim(self):
        from submissions.claiming import claim_submission
        return claim_submission(self.submission.id, 'w1')

    def _envelope(self, token, verdict='Accepted'):
        return {
            'kind': 'result',
            'submission_id': self.submission.id,
            'claim_token': str(token),
            'worker_id': 'w1',
            'verdict': verdict,
            'runtime_ms': 7,
            'memory_kb': 12,
            'cases': [
                {'index': 1, 'status': 'Accepted', 'runtime_ms': 7,
                 'actual_output': '', 'error_message': ''},
                {'index': 2, 'status': 'Accepted', 'runtime_ms': 5,
                 'actual_output': '', 'error_message': ''},
            ],
        }

    def test_outcome_persisted_and_duplicate_discarded(self):
        from submissions.result_queue import push_envelope
        from submissions.results import process_envelope

        token = self._claim()
        conn = MagicMock()
        raw = json.dumps(self._envelope(token)).encode()
        self.assertEqual(process_envelope(raw, conn), 'ok')

        self.submission.refresh_from_db()
        self.assertEqual(self.submission.judge_state, 'DONE')
        self.assertEqual(self.submission.status, 'Accepted')
        self.assertEqual(self.submission.runtime, 7)
        self.assertEqual(self.submission.test_results.count(), 2)
        self.assertIn(self.problem, list(self.user.solved_problems.all()))

        # Redelivery (at-least-once) must not duplicate anything.
        raw2 = json.dumps(self._envelope(token)).encode()
        self.assertEqual(process_envelope(raw2, conn), 'ok')
        self.assertEqual(self.submission.test_results.count(), 2)
        self.assertIn(self.problem, list(self.user.solved_problems.all()))

    def test_stale_token_envelope_discarded(self):
        from submissions.results import process_envelope

        token_a = self._claim()
        from submissions.claiming import requeue_claim, claim_submission
        requeue_claim(self.submission.id)
        token_b = claim_submission(self.submission.id, 'w2')
        conn = MagicMock()
        process_envelope(
            json.dumps(self._envelope(token_a)).encode(), conn,
        )
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.judge_state, 'JUDGING')
        self.assertEqual(self.submission.status, 'Pending')
        self.assertEqual(self.submission.claim_token, token_b)
        self.assertEqual(self.submission.test_results.count(), 0)

    @patch('submissions.results.sleep_backoff')
    def test_infra_envelope_requeues_then_fails(self, _sleep):
        from submissions.results import process_envelope

        token = self._claim()
        conn = MagicMock()
        conn.incr.return_value = 1
        with patch('submissions.judge_queue.enqueue_judge') as enqueue:
            process_envelope(json.dumps({
                'kind': 'infra', 'submission_id': self.submission.id,
                'claim_token': str(token), 'error': 'docker down',
            }).encode(), conn)
            enqueue.assert_called_once_with(self.submission.id)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.judge_state, 'QUEUED')

        # New claim wins after the requeue; third attempt is terminal.
        from submissions.claiming import claim_submission
        token2 = claim_submission(self.submission.id, 'w1')
        conn2 = MagicMock()
        conn2.incr.return_value = 3
        process_envelope(json.dumps({
            'kind': 'infra', 'submission_id': self.submission.id,
            'claim_token': str(token2), 'error': 'docker still down',
        }).encode(), conn2)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.judge_state, 'FAILED')
        self.assertEqual(self.submission.status, 'System Error')


class ResultQueueHelperTests(TestCase):
    def test_push_fetch_ack_and_dead_letter_roundtrip(self):
        from submissions import result_queue as rq

        conn = MagicMock()
        conn.brpoplpush.return_value = b'msg-1'

        rq.push_envelope(conn, {'submission_id': 1, 'x': '中'})
        pushed = conn.lpush.call_args[0]
        self.assertEqual(pushed[0], 'judge:result')
        payload = json.loads(pushed[1])
        self.assertEqual(payload['x'], '中')

        self.assertEqual(rq.fetch_for_processing(conn, timeout_secs=2),
                         b'msg-1')
        conn.brpoplpush.assert_called_with(
            'judge:result', 'judge:result:processing', timeout=2,
        )

        rq.ack(conn, b'msg-1')
        conn.lrem.assert_called_with('judge:result:processing', 1, b'msg-1')

        rq.dead_letter(conn, b'{"submission_id": 5}', 'boom')
        dead = conn.lpush.call_args[0]
        self.assertEqual(dead[0], 'judge:result:dead')
        self.assertIn('boom', json.loads(dead[1])['_dead_letter_reason'])

    def test_recover_processing_moves_each_message(self):
        from submissions import result_queue as rq

        conn = MagicMock()
        conn.rpoplpush.side_effect = [b'a', b'b', None]
        self.assertEqual(rq.recover_processing(conn), 2)


@override_settings(
    OJ_WORKER_DBLESS=True,
    JUDGE_API_BASE='https://judge-api.invalid',
    JUDGE_INTERNAL_TOKEN=TEST_TOKEN,
    OJ_JUDGE_HEARTBEAT_SECS=600,
)
class DblessTaskGlueTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='dbless-task-user', password='safe-test-password',
        )
        self.problem = _make_problem(self.user)
        self.submission = Submission.objects.create(
            problem=self.problem, user=self.user,
            language='Python', code='print(1)',
        )

    def _fake_job(self):
        job = MagicMock()
        job.origin = 'judge:queue'
        job.connection = MagicMock()
        return job

    def test_success_pushes_result_envelope(self):
        from submissions.tasks import judge_submission_task

        token = '11111111-1111-1111-1111-111111111111'
        bundle = {
            'claimable': True, 'claim_token': token,
            'submission_id': self.submission.id, 'language': 'Python',
            'code': 'print(1)', 'user_id': self.user.id,
            'is_contest': False, 'time_limit_ms': 1000,
            'memory_limit_mb': 256, 'subprocess_timeout_sec': 5,
            'cases': [{'index': 1, 'input': '', 'expected': ''}],
        }
        outcome = {
            'verdict': 'Accepted', 'runtime_ms': 4, 'memory_kb': 9,
            'cases': [{'index': 1, 'status': 'Accepted', 'runtime_ms': 4,
                       'actual_output': '', 'error_message': ''}],
        }
        job = self._fake_job()
        with patch('submissions.tasks.get_current_job', return_value=job), \
             patch('submissions.worker_api.JudgeApiClient') as Api, \
             patch('submissions.judge_core.judge_spec',
                   return_value=outcome) as spec_mock:
            Api.return_value.claim.return_value = bundle
            Api.return_value.heartbeat.return_value = True
            result = judge_submission_task(self.submission.id)

        self.assertEqual(result, self.submission.id)
        spec_mock.assert_called_once()
        pushed = job.connection.lpush.call_args[0]
        self.assertEqual(pushed[0], 'judge:result')
        env = json.loads(pushed[1])
        self.assertEqual(env['kind'], 'result')
        self.assertEqual(env['verdict'], 'Accepted')
        self.assertEqual(env['claim_token'], token)

    def test_claim_loss_acks_silently(self):
        from submissions.tasks import judge_submission_task

        job = self._fake_job()
        with patch('submissions.tasks.get_current_job', return_value=job), \
             patch('submissions.worker_api.JudgeApiClient') as Api:
            Api.return_value.claim.return_value = {'claimable': False}
            result = judge_submission_task(self.submission.id)
        self.assertIsNone(result)
        job.connection.lpush.assert_not_called()
        # The row was never claimed (no DB in the task path at all).
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.judge_state, 'PENDING')
