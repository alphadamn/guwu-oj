from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from problems.models import Problem, TestCase as ProblemTestCase
from submissions.models import Submission


class JudgeTimeoutVerdictTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(
            username="judge-timeout-user",
            password="safe-test-password",
        )
        self.problem = Problem.objects.create(
            title="Timeout verdict",
            description="",
            input_format="",
            output_format="",
            time_limit=1000,
            memory_limit=256,
            created_by=user,
        )
        ProblemTestCase.objects.create(
            problem=self.problem,
            input_data="",
            expected_output="",
        )
        self.submission = Submission.objects.create(
            problem=self.problem,
            user=user,
            language="Python",
            code="print('unused')",
        )

    def _judge_with_result(self, returncode, elapsed_ms):
        from submissions.judge import judge_submission

        result = SimpleNamespace(returncode=returncode, stdout="", stderr="")
        with patch("submissions.judge.JudgeContainer") as container_class:
            container = container_class.return_value.__enter__.return_value
            container.exec.return_value = result
            with patch(
                "submissions.judge.SandboxRunner._parse_time_stderr",
                return_value=(elapsed_ms, 0),
            ):
                judge_submission(self.submission.id)

        self.submission.refresh_from_db()
        return self.submission.test_results.get()

    def test_nonzero_exit_at_limit_is_time_limit_exceeded(self):
        case_result = self._judge_with_result(returncode=11, elapsed_ms=1000)

        self.assertEqual(self.submission.status, "Time Limit Exceeded")
        self.assertEqual(case_result.status, "Time Limit Exceeded")

    def test_nonzero_exit_below_limit_remains_runtime_error(self):
        case_result = self._judge_with_result(returncode=11, elapsed_ms=999)

        self.assertEqual(self.submission.status, "Runtime Error")
        self.assertEqual(case_result.status, "Runtime Error")


class CompileErrorResultTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(
            username="compile-error-user",
            password="safe-test-password",
        )
        self.problem = Problem.objects.create(
            title="Compile error result",
            description="",
            input_format="",
            output_format="",
            time_limit=1000,
            memory_limit=256,
            created_by=user,
        )
        self.test_case = ProblemTestCase.objects.create(
            problem=self.problem,
            input_data="",
            expected_output="expected",
        )
        self.submission = Submission.objects.create(
            problem=self.problem,
            user=user,
            language="C++",
            code="int main( {",
        )

    def test_compile_error_persists_first_case_diagnostic(self):
        from submissions.judge import judge_submission

        compiler_message = "main.cpp:1: error: expected declaration"
        with patch("submissions.judge.JudgeContainer") as container_class:
            container = container_class.return_value.__enter__.return_value
            container.exec.return_value = SimpleNamespace(
                returncode=1,
                stdout="",
                stderr=compiler_message,
            )
            judge_submission(self.submission.id)

        self.submission.refresh_from_db()
        result = self.submission.test_results.get()
        self.assertEqual(self.submission.status, "Compile Error")
        self.assertEqual(result.case_index, 1)
        self.assertEqual(result.status, "Skipped")
        self.assertEqual(result.test_case_id, self.test_case.id)
        self.assertEqual(result.error_message, compiler_message)
        self.assertEqual(result.actual_output, compiler_message)


class JudgeContainerSecurityTests(TestCase):
    def test_committed_profile_allows_unconfined_lifecycle_signals(self):
        from pathlib import Path

        profile = (
            Path(__file__).resolve().parent.parent
            / 'docker' / 'judge' / 'apparmor-profile'
        ).read_text()

        self.assertIn('signal (receive) peer=unconfined,', profile)
        self.assertIn('deny signal peer=oj-judge,', profile)
        self.assertNotIn('deny signal,', profile)

    @patch('submissions.sandbox.ensure_judge_image_available')
    @patch('submissions.sandbox.ensure_docker_ready')
    @patch('submissions.sandbox.subprocess.run')
    def test_container_uses_configured_apparmor_profile(
        self, run, _docker_ready, _image_available,
    ):
        from submissions.sandbox import JudgeContainer

        run.return_value = SimpleNamespace(returncode=0, stdout='judge-id\n', stderr='')
        with override_settings(OJ_DOCKER_APPARMOR_PROFILE='oj-judge-test'):
            container = JudgeContainer('/tmp/oj-judge-test', 64, 'oj-python:latest')
            container.__enter__()

        command = run.call_args.args[0]
        self.assertIn('apparmor=oj-judge-test', command)
        self.assertIn('/dev/null:rw', command)
        container.__exit__(None, None, None)

