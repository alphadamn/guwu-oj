"""Tests for Phase 3: DB-less workers, internal claim API, result queue.

Covers the pure judging core, the internal claim/heartbeat API, the
fencing-protected consumer writeback, reliable-queue helpers and the
DB-less task glue (HTTP claim -> judge core -> result envelope).
"""

import json
import os
import shutil
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, Client, override_settings

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

    def test_core_pulls_cases_lazily_and_reports_each(self):
        from submissions.judge_core import judge_spec

        all_cases = [
            {'index': 1, 'input': '', 'expected': ''},
            {'index': 2, 'input': '', 'expected': ''},
        ]
        calls = []

        def loader(offset, limit):
            calls.append((offset, limit))
            return all_cases[offset:offset + limit]

        specs = {
            'submission_id': 999003, 'language': 'Python', 'code': 'print("")',
            'user_id': self.user.id, 'time_limit_ms': 1000,
            'memory_limit_mb': 256, 'total_cases': 2, 'cases': [],
        }
        done = []
        with patch('submissions.judge.JudgeContainer') as container_class, \
             patch('submissions.judge.container_pool.acquire',
                   return_value=None), \
             patch('submissions.judge.SandboxRunner._parse_time_stderr',
                   return_value=(3, 0)):
            container = container_class.return_value.__enter__.return_value
            container.exec.return_value = SimpleNamespace(
                returncode=0, stdout='\n', stderr='',
            )
            outcome = judge_spec(
                specs, global_timeout_sec=5,
                case_loader=loader, on_case_done=done.append,
            )

        self.assertEqual(outcome['verdict'], 'Accepted')
        self.assertEqual([c['index'] for c in outcome['cases']], [1, 2])
        # Every case finished is reported as it completes, in order.
        self.assertEqual([c['index'] for c in done], [1, 2])
        # One batch sufficed, so the loader is not re-hit per case. The
        # request is clipped to the cases that exist, because a cache that
        # only answers complete slices would miss a full batch past the end
        # and force a network round trip in front of the final cases.
        self.assertEqual(calls, [(0, 2)])

    def test_case_feed_prefetches_ahead_of_the_consumer(self):
        from submissions.judge_core import _CaseFeed

        cases = [
            {'index': i, 'input': '', 'expected': ''} for i in range(1, 5)
        ]
        calls = []
        second_fetch_started = threading.Event()

        def loader(offset, limit):
            calls.append(offset)
            if offset:
                second_fetch_started.set()
            return cases[offset:offset + limit]

        feed = _CaseFeed([], len(cases), loader, batch=2)
        try:
            self.assertEqual(feed.case(1).case_index, 1)
            # Batch two is on the wire while case two is still unread: a
            # synchronous feed could not fetch again before the caller
            # asked for a case it did not already hold.
            self.assertTrue(second_fetch_started.wait(2.0))
            self.assertEqual(feed.case(2).case_index, 2)
        finally:
            feed.close()
        # Both batches came from one call each; the tail was not re-fetched.
        self.assertEqual(calls, [0, 2])

    def test_case_feed_surfaces_a_loader_failure(self):
        from submissions.judge_core import _CaseFeed

        def loader(offset, limit):
            raise RuntimeError('test data feed exploded')

        feed = _CaseFeed([], 1, loader, batch=1)
        try:
            with self.assertRaises(RuntimeError):
                feed.case(1)
        finally:
            feed.close()


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
        # Only the batched flow gets the cache key: computing it reads every
        # test case, which a worker that inlines them does not need.
        self.assertEqual(data['data_key'], '')

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

    def test_batched_claim_omits_cases_and_serves_slices(self):
        data = self._post('/internal/judge/claim/', {
            'submission_id': self.submission.id, 'worker_id': 'w1',
            'batched_cases': True,
        }).json()
        self.assertTrue(data['claimable'])
        self.assertEqual(data['total_cases'], 3)
        self.assertEqual(data['cases'], [])

        from problems.fingerprint import compute_test_data_fingerprint
        self.assertEqual(
            data['data_key'], compute_test_data_fingerprint(self.problem.id),
        )

        first = self._post('/internal/judge/cases/', {
            'submission_id': self.submission.id,
            'claim_token': data['claim_token'], 'offset': 0, 'limit': 2,
        }).json()
        self.assertEqual(first['total'], 3)
        self.assertEqual([c['index'] for c in first['cases']], [1, 2])

        rest = self._post('/internal/judge/cases/', {
            'submission_id': self.submission.id,
            'claim_token': data['claim_token'], 'offset': 2, 'limit': 2,
        }).json()
        self.assertEqual([c['index'] for c in rest['cases']], [3])

    def test_cases_endpoint_rejects_a_token_that_no_longer_owns(self):
        data = self._post('/internal/judge/claim/', {
            'submission_id': self.submission.id, 'worker_id': 'w1',
            'batched_cases': True,
        }).json()

        from submissions.claiming import requeue_claim
        requeue_claim(self.submission.id)

        resp = self._post('/internal/judge/cases/', {
            'submission_id': self.submission.id,
            'claim_token': data['claim_token'], 'offset': 0, 'limit': 2,
        })
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(resp.json()['claimable'])


@override_settings(CACHES={
    'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'},
})
class TestDataFingerprintTests(TestCase):
    """The claim's ``data_key``: stable for unchanged data, moved by edits."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='fp-user', password='safe-test-password',
        )
        self.problem = _make_problem(self.user, cases=2)
        cache.clear()

    def _fingerprint(self, problem=None):
        from problems.fingerprint import compute_test_data_fingerprint
        return compute_test_data_fingerprint((problem or self.problem).id)

    def test_repeated_calls_return_the_memoised_digest(self):
        first = self._fingerprint()
        self.assertEqual(len(first), 64)
        self.assertEqual(self._fingerprint(), first)

    def test_identical_text_in_another_problem_is_a_different_key(self):
        # The digest covers row identity as well as text, so two problems
        # holding the same strings cannot share a cache entry.
        self.assertNotEqual(
            self._fingerprint(_make_problem(self.user)), self._fingerprint(),
        )

    def test_editing_a_case_moves_the_key(self):
        before = self._fingerprint()
        case = self.problem.test_cases.first()
        case.expected_output = 'changed'
        case.save()
        self.assertNotEqual(self._fingerprint(), before)

    def test_deleting_a_case_moves_the_key(self):
        before = self._fingerprint()
        self.problem.test_cases.first().delete()
        self.assertNotEqual(self._fingerprint(), before)

    def test_bulk_inserted_cases_move_the_key(self):
        # Importer scripts use bulk_create, which fires no signals: the shape
        # check on read has to catch it.
        before = self._fingerprint()
        ProblemTestCase.objects.bulk_create([
            ProblemTestCase(
                problem=self.problem, input_data='1', expected_output='2',
            ),
        ])
        self.assertNotEqual(self._fingerprint(), before)

    def test_problem_without_cases_has_no_key(self):
        self.assertEqual(self._fingerprint(_make_problem(self.user, cases=0)), '')


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

    def test_partial_envelope_writes_one_case_and_publishes(self):
        from submissions.results import process_envelope

        token = self._claim()
        conn = MagicMock()
        with patch('submissions.realtime.publish_submission_changed') as notify:
            self.assertEqual(process_envelope(json.dumps({
                'kind': 'partial', 'submission_id': self.submission.id,
                'claim_token': str(token), 'worker_id': 'w1',
                'case': {'index': 1, 'status': 'Accepted', 'runtime_ms': 4,
                         'actual_output': '', 'error_message': ''},
            }).encode(), conn), 'ok')
            notify.assert_called_once_with(self.submission.id)

        self.submission.refresh_from_db()
        self.assertEqual(self.submission.judge_state, 'JUDGING')
        self.assertEqual(self.submission.test_results.count(), 1)
        self.assertEqual(self.submission.test_results.get().case_index, 1)

        # The terminal envelope stays authoritative and rewrites every case.
        process_envelope(
            json.dumps(self._envelope(token)).encode(), conn,
        )
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.test_results.count(), 2)
        self.assertEqual(self.submission.judge_state, 'DONE')

    def test_partial_with_stale_token_is_dropped(self):
        from submissions.results import process_envelope

        token_a = self._claim()
        from submissions.claiming import claim_submission, requeue_claim
        requeue_claim(self.submission.id)
        claim_submission(self.submission.id, 'w2')
        conn = MagicMock()
        with patch('submissions.realtime.publish_submission_changed') as notify:
            self.assertEqual(process_envelope(json.dumps({
                'kind': 'partial', 'submission_id': self.submission.id,
                'claim_token': str(token_a), 'worker_id': 'w1',
                'case': {'index': 1, 'status': 'Accepted', 'runtime_ms': 4},
            }).encode(), conn), 'ok')
            notify.assert_not_called()
        self.assertEqual(self.submission.test_results.count(), 0)

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

    def _fake_broker(self):
        return MagicMock()

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
        broker = self._fake_broker()
        with patch('submissions.tasks.broker_client', return_value=broker), \
             patch('submissions.worker_api.JudgeApiClient') as Api, \
             patch('submissions.judge_core.judge_spec',
                   return_value=outcome) as spec_mock:
            Api.return_value.claim.return_value = bundle
            Api.return_value.heartbeat.return_value = True
            result = judge_submission_task(self.submission.id)

        self.assertEqual(result, self.submission.id)
        spec_mock.assert_called_once()
        pushed = broker.lpush.call_args[0]
        self.assertEqual(pushed[0], 'judge:result')
        env = json.loads(pushed[1])
        self.assertEqual(env['kind'], 'result')
        self.assertEqual(env['verdict'], 'Accepted')
        self.assertEqual(env['claim_token'], token)

    def test_claim_loss_acks_silently(self):
        from submissions.tasks import judge_submission_task

        broker = self._fake_broker()
        with patch('submissions.tasks.broker_client', return_value=broker), \
             patch('submissions.worker_api.JudgeApiClient') as Api:
            Api.return_value.claim.return_value = {'claimable': False}
            result = judge_submission_task(self.submission.id)
        self.assertIsNone(result)
        broker.lpush.assert_not_called()
        # The row was never claimed (no DB in the task path at all).
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.judge_state, 'PENDING')

    def test_second_run_reads_test_data_from_disk(self):
        from submissions.tasks import judge_submission_task

        root = tempfile.mkdtemp(prefix='oj-case-cache-')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        row = {'index': 1, 'input': '1 2', 'expected': '3'}
        bundle = {
            'claimable': True, 'claim_token': '22222222-2222-2222-2222-222222222222',
            'submission_id': self.submission.id, 'language': 'Python',
            'code': 'print(1)', 'user_id': self.user.id,
            'is_contest': False, 'time_limit_ms': 1000,
            'memory_limit_mb': 256, 'subprocess_timeout_sec': 5,
            'total_cases': 1, 'data_key': 'c' * 64, 'cases': [],
        }
        outcome = {'verdict': 'Accepted', 'runtime_ms': 1, 'memory_kb': 1,
                   'cases': []}
        broker = self._fake_broker()
        with override_settings(OJ_CASE_CACHE_DIR=root,
                               OJ_CASE_CACHE_MAX_SIZE='0'), \
             patch('submissions.tasks.broker_client', return_value=broker), \
             patch('submissions.worker_api.JudgeApiClient') as Api, \
             patch('submissions.judge_core.judge_spec',
                   return_value=outcome) as spec_mock:
            Api.return_value.claim.return_value = bundle
            Api.return_value.heartbeat.return_value = True
            Api.return_value.fetch_cases.return_value = [row]
            judge_submission_task(self.submission.id)
            loader = spec_mock.call_args.kwargs['case_loader']

            self.assertEqual(loader(0, 1), [row])
            self.assertEqual(Api.return_value.fetch_cases.call_count, 1)

            # Second submission of the same problem: served from disk.
            self.assertEqual(loader(0, 1), [row])
            self.assertEqual(Api.return_value.fetch_cases.call_count, 1)


class CaseStoreTests(SimpleTestCase):
    """Local test data copy: round trip, isolation and the size cap."""

    def setUp(self):
        from submissions.case_store import CaseStore

        self.CaseStore = CaseStore
        self.root = tempfile.mkdtemp(prefix='oj-case-store-')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _store(self, key='a' * 64, max_bytes=0):
        return self.CaseStore(key, directory=self.root, max_bytes=max_bytes)

    def test_round_trip_and_miss(self):
        rows = [{'index': 1, 'input': '1 2', 'expected': '3'},
                {'index': 2, 'input': '4', 'expected': '4'}]
        store = self._store()
        self.assertIsNone(store.get(0, 2))
        store.put(rows)
        self.assertEqual(store.get(0, 2), rows)
        self.assertEqual(store.get(1, 1), [rows[1]])
        # Asking past the stored range is a miss, not a short answer.
        self.assertIsNone(store.get(2, 1))
        # A different problem's entries are not visible under this key.
        self.assertIsNone(self._store(key='b' * 64).get(0, 2))

    def test_unusable_key_or_directory_is_disabled(self):
        from submissions.case_store import parse_size

        self.assertEqual(parse_size('2G'), 2 * 1024 ** 3)
        self.assertEqual(parse_size('nonsense'), 0)
        rows = [{'index': 1, 'input': '', 'expected': ''}]
        for store in (
            self._store(key=''),
            self._store(key='../../etc'),
            self.CaseStore('a' * 64, directory='', max_bytes=0),
        ):
            self.assertFalse(store.enabled)
            store.put(rows)
            self.assertIsNone(store.get(0, 1))
        self.assertEqual(os.listdir(self.root), [])

    def test_prune_evicts_the_least_recently_used_problem(self):
        from submissions.case_store import _dir_size

        cold, hot = 'a' * 64, 'b' * 64
        row = {'index': 1, 'input': 'x' * 512, 'expected': 'y' * 512}
        self._store(key=cold).put([row])
        cap = _dir_size(os.path.join(self.root, cold)) + 8

        with patch('submissions.case_store._PRUNE_EVERY_BYTES', 1):
            self._store(key=hot, max_bytes=cap).put([row])

        self.assertEqual(os.listdir(self.root), [hot])

