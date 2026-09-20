"""Tests for the Phase 2 claim / lease / fence machinery.

Covers:
* atomic claim (including a real two-thread race),
* heartbeat renewal and loss,
* fenced terminal writeback (stale tokens affect zero rows),
* dispatch gate (terminal rows cannot be re-enqueued),
* zombie reaping of stale JUDGING / QUEUED rows,
* end-to-end duplicate delivery through the RQ task (exactly one verdict
  transition and one set of side effects),
* bounded infra-failure retries,
* idempotent per-case result writes.
"""

import threading
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from problems.models import Problem, TestCase as ProblemTestCase
from submissions import claiming
from submissions.claiming import (
    Claim,
    ClaimLostError,
    claim_submission,
    finalize_claim,
    heartbeat_claim,
    mark_queued,
    reap_stale_claims,
    requeue_claim,
)
from submissions.models import Submission, SubmissionTestResult


def _make_problem(user, title='Claim fixture', cases=2):
    problem = Problem.objects.create(
        title=title, description='', input_format='', output_format='',
        time_limit=1000, memory_limit=256, created_by=user,
    )
    for _ in range(cases):
        ProblemTestCase.objects.create(
            problem=problem, input_data='', expected_output='',
        )
    return problem


def _make_submission(user, problem, language='Python', code='print(1)'):
    return Submission.objects.create(
        problem=problem, user=user, language=language, code=code,
    )


class ClaimTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='claim-user', password='safe-test-password',
        )
        self.problem = _make_problem(self.user)
        self.submission = _make_submission(self.user, self.problem)

    def test_claim_wins_once(self):
        token1 = claim_submission(self.submission.id, 'worker-a')
        self.assertIsNotNone(token1)

        token2 = claim_submission(self.submission.id, 'worker-b')
        self.assertIsNone(token2)

        self.submission.refresh_from_db()
        self.assertEqual(self.submission.judge_state, 'JUDGING')
        self.assertEqual(self.submission.worker_id, 'worker-a')
        self.assertEqual(self.submission.claim_token, token1)
        self.assertIsNotNone(self.submission.claimed_at)
        self.assertIsNotNone(self.submission.heartbeat_at)

    def test_terminal_row_cannot_be_claimed(self):
        token = claim_submission(self.submission.id, 'worker-a')
        self.assertTrue(finalize_claim(
            self.submission.id, token, 'Accepted', runtime=1, memory=2,
        ))
        # Duplicate delivery after the verdict must be absorbed.
        self.assertIsNone(claim_submission(self.submission.id, 'worker-b'))
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.judge_state, 'DONE')
        self.assertEqual(self.submission.status, 'Accepted')

    def test_heartbeat_renews_only_for_live_token(self):
        token = claim_submission(self.submission.id, 'worker-a')
        self.assertTrue(heartbeat_claim(self.submission.id, token))

        import uuid
        self.assertFalse(
            heartbeat_claim(self.submission.id, uuid.uuid4()),
        )

    def test_heartbeat_fails_after_requeue(self):
        token = claim_submission(self.submission.id, 'worker-a')
        self.assertTrue(requeue_claim(self.submission.id))
        self.assertFalse(heartbeat_claim(self.submission.id, token))

        self.submission.refresh_from_db()
        self.assertEqual(self.submission.judge_state, 'QUEUED')
        self.assertIsNone(self.submission.claim_token)
        self.assertEqual(self.submission.worker_id, '')


class FenceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='fence-user', password='safe-test-password',
        )
        self.problem = _make_problem(self.user)
        self.submission = _make_submission(self.user, self.problem)

    def test_stale_token_cannot_overwrite_new_owner(self):
        # Owner A wins, then dies; reaper requeues and owner B wins.
        token_a = claim_submission(self.submission.id, 'worker-a')
        self.assertTrue(requeue_claim(self.submission.id))
        token_b = claim_submission(self.submission.id, 'worker-b')
        self.assertNotEqual(token_a, token_b)

        # A's late writeback is fenced off...
        self.assertFalse(finalize_claim(
            self.submission.id, token_a, 'Accepted', runtime=9, memory=9,
        ))
        # ...and B still owns a pristine JUDGING row.
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.judge_state, 'JUDGING')
        self.assertEqual(self.submission.status, 'Pending')
        self.assertEqual(self.submission.claim_token, token_b)

        # B finalises normally.
        self.assertTrue(finalize_claim(
            self.submission.id, token_b, 'Wrong Answer', runtime=5,
        ))
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.judge_state, 'DONE')
        self.assertEqual(self.submission.status, 'Wrong Answer')
        self.assertIsNotNone(self.submission.finished_at)

    def test_failed_finalize_marks_failed_state(self):
        token = claim_submission(self.submission.id, 'worker-a')
        self.assertTrue(finalize_claim(
            self.submission.id, token, 'System Error', runtime=0, failed=True,
        ))
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.judge_state, 'FAILED')
        self.assertEqual(self.submission.status, 'System Error')


class ConcurrentClaimRaceTests(TransactionTestCase):
    """Two real DB connections racing for one submission: exactly one wins."""

    reset_sequences = True

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='race-user', password='safe-test-password',
        )
        self.problem = _make_problem(self.user)
        self.submission = _make_submission(self.user, self.problem)

    def test_two_threads_exactly_one_winner(self):
        barrier = threading.Barrier(2)
        results = [None, None]

        def run(idx, worker):
            from django.db import close_old_connections
            close_old_connections()
            barrier.wait()
            try:
                results[idx] = claim_submission(self.submission.id, worker)
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=run, args=(0, 'worker-a')),
            threading.Thread(target=run, args=(1, 'worker-b')),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        winners = [r for r in results if r is not None]
        self.assertEqual(len(winners), 1)
        self.assertIsNone(results[0] if results[1] is not None else results[1])
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.judge_state, 'JUDGING')


class DispatchGateTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='gate-user', password='safe-test-password',
        )
        self.problem = _make_problem(self.user)
        self.submission = _make_submission(self.user, self.problem)

    @override_settings(OJ_CENTRAL_QUEUE=False, OJ_MULTI_JUDGE_ENABLED=False)
    def test_terminal_row_enqueue_is_a_noop(self):
        token = claim_submission(self.submission.id, 'worker-a')
        finalize_claim(self.submission.id, token, 'Accepted')

        from submissions.judge_queue import enqueue_judge
        with patch('submissions.judge_queue.get_queue') as get_queue:
            job = enqueue_judge(self.submission.id)
        self.assertIsNone(job)
        get_queue.assert_not_called()

    def test_mark_queued_transitions_pending(self):
        self.assertTrue(mark_queued(self.submission.id))
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.judge_state, 'QUEUED')
        # Idempotent: still QUEUED, still dispatchable.
        self.assertTrue(mark_queued(self.submission.id))


class ReaperTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='reaper-user', password='safe-test-password',
        )
        self.problem = _make_problem(self.user)

    def _submission(self, state, heartbeat_age=None, age=None):
        s = _make_submission(self.user, self.problem)
        s.judge_state = state
        s.save(update_fields=['judge_state'])
        if heartbeat_age is not None:
            Submission.objects.filter(pk=s.id).update(
                heartbeat_at=timezone.now() - timedelta(seconds=heartbeat_age),
            )
        if age is not None:
            Submission.objects.filter(pk=s.id).update(
                created_at=timezone.now() - timedelta(seconds=age),
            )
        return s

    def test_reaps_stale_judging_and_lost_queued_only(self):
        stale_judging = self._submission('JUDGING', heartbeat_age=600)
        fresh_judging = self._submission('JUDGING', heartbeat_age=10)
        lost_queued = self._submission('QUEUED', age=900)
        fresh_queued = self._submission('QUEUED', age=10)
        done = self._submission('DONE', heartbeat_age=9999)

        requeued = reap_stale_claims(
            judging_timeout_secs=300, queued_timeout_secs=600,
        )
        self.assertEqual(set(requeued), {stale_judging.id, lost_queued.id})

        stale_judging.refresh_from_db()
        fresh_judging.refresh_from_db()
        lost_queued.refresh_from_db()
        fresh_queued.refresh_from_db()
        done.refresh_from_db()
        self.assertEqual(stale_judging.judge_state, 'QUEUED')
        self.assertIsNone(stale_judging.claim_token)
        self.assertEqual(fresh_judging.judge_state, 'JUDGING')
        self.assertEqual(lost_queued.judge_state, 'QUEUED')
        self.assertEqual(fresh_queued.judge_state, 'QUEUED')
        self.assertEqual(done.judge_state, 'DONE')

    def test_reap_does_not_touch_freshly_claimed(self):
        self._submission('JUDGING', heartbeat_age=299)
        self.assertEqual(
            reap_stale_claims(judging_timeout_secs=300), [],
        )

    @patch('submissions.realtime.publish_submission_changed')
    @patch('submissions.judge_queue.enqueue_judge')
    def test_command_one_shot_re_enqueues(self, enqueue, _publish):
        stale = self._submission('JUDGING', heartbeat_age=600)
        from django.core.management import call_command
        call_command('reap_stale_judgments')
        enqueue.assert_called_once_with(stale.id)


@override_settings(OJ_JUDGE_HEARTBEAT_SECS=600)
class DuplicateDeliveryTaskTests(TestCase):
    """End-to-end through judge_submission_task with Docker mocked out."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='dup-delivery-user', password='safe-test-password',
        )
        self.problem = _make_problem(self.user, cases=2)
        self.submission = _make_submission(self.user, self.problem)

    def _patch_container(self, stdout=''):
        # container_pool.acquire returns None -> per-submission fallback
        # container path, exactly like tests.test_judge.
        container_patcher = patch('submissions.judge.JudgeContainer')
        self._container_class = container_patcher.start()
        self.addCleanup(container_patcher.stop)
        pool_patcher = patch(
            'submissions.judge.container_pool.acquire', return_value=None,
        )
        pool_patcher.start()
        self.addCleanup(pool_patcher.stop)
        container = self._container_class.return_value.__enter__.return_value
        container.exec.return_value = SimpleNamespace(
            returncode=0, stdout=stdout, stderr='',
        )
        time_patcher = patch(
            'submissions.judge.SandboxRunner._parse_time_stderr',
            return_value=(5, 0),
        )
        time_patcher.start()
        self.addCleanup(time_patcher.stop)

    def test_duplicate_delivery_judges_once(self):
        from submissions.tasks import judge_submission_task

        self._patch_container()
        first = judge_submission_task(self.submission.id)
        self.assertEqual(first, self.submission.id)

        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status, 'Accepted')
        self.assertEqual(self.submission.judge_state, 'DONE')
        self.assertEqual(self.submission.test_results.count(), 2)

        # The exact duplicate must be ACKed without touching results.
        second = judge_submission_task(self.submission.id)
        self.assertIsNone(second)
        self.assertEqual(self.submission.test_results.count(), 2)

        # Side effects: solved M2M added exactly once (set semantics).
        self.assertIn(
            self.problem,
            list(self.user.solved_problems.all()),
        )

    def test_stale_claim_mid_judgement_is_discarded(self):
        # Owner A claims, then loses the lease before finalising.
        token_a = claim_submission(self.submission.id, 'worker-a')
        token_b = claim_submission(self.submission.id, 'worker-b')
        self.assertIsNone(token_b)  # A still owns it
        self.assertTrue(requeue_claim(self.submission.id))
        token_b = claim_submission(self.submission.id, 'worker-b')
        self.assertIsNotNone(token_b)

        self._patch_container()
        claim_a = Claim(self.submission.id, token_a, 'worker-a',
                        interval_secs=600)
        with self.assertRaises(ClaimLostError):
            from submissions.judge import judge_submission
            judge_submission(self.submission.id, claim=claim_a)

        self.submission.refresh_from_db()
        # A could not write a verdict or any terminal state.
        self.assertEqual(self.submission.status, 'Pending')
        self.assertEqual(self.submission.judge_state, 'JUDGING')
        self.assertEqual(self.submission.claim_token, token_b)
        self.assertNotIn(self.problem, list(self.user.solved_problems.all()))


class CaseResultIdempotencyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='case-idemp-user', password='safe-test-password',
        )
        self.problem = _make_problem(self.user, cases=1)
        self.tc = self.problem.test_cases.first()
        self.submission = _make_submission(self.user, self.problem)

    def test_same_case_written_twice_collapses_to_one_row(self):
        from submissions.judge import save_case_result

        save_case_result(
            self.submission, self.tc, 1, 'Accepted', 5, '', '', '',
        )
        save_case_result(
            self.submission, self.tc, 1, 'Wrong Answer', 6, 'x', '', '',
        )
        self.assertEqual(self.submission.test_results.count(), 1)
        row = self.submission.test_results.get(case_index=1)
        self.assertEqual(row.status, 'Wrong Answer')
        self.assertEqual(row.runtime, 6)


@override_settings(OJ_JUDGE_HEARTBEAT_SECS=600)
class InfraRetryTaskTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='retry-user', password='safe-test-password',
        )
        self.problem = _make_problem(self.user)
        self.submission = _make_submission(self.user, self.problem)

    def _run_task_with_failure(self, attempt_value):
        from submissions.sandbox import DockerNotAvailableError
        from submissions.tasks import judge_submission_task

        fake_redis = MagicMock()
        fake_redis.incr.return_value = attempt_value
        with patch('submissions.judge.judge_submission',
                   side_effect=DockerNotAvailableError('docker down')), \
             patch('django_redis.get_redis_connection',
                   return_value=fake_redis), \
             patch('submissions.judge_queue.enqueue_judge') as enqueue, \
             patch('submissions.claiming.sleep_backoff'):
            judge_submission_task(self.submission.id)
        return fake_redis, enqueue

    def test_first_failures_are_requeued(self):
        fake_redis, enqueue = self._run_task_with_failure(1)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.judge_state, 'QUEUED')
        self.assertIsNone(self.submission.claim_token)
        enqueue.assert_called_once_with(self.submission.id)
        fake_redis.expire.assert_called_once()
        fake_redis.delete.assert_not_called()

    def test_last_failure_is_terminal_failed(self):
        fake_redis, enqueue = self._run_task_with_failure(3)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.judge_state, 'FAILED')
        self.assertEqual(self.submission.status, 'System Error')
        enqueue.assert_not_called()
        fake_redis.delete.assert_called_once()
