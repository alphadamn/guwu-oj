"""Tests for bounded stdout/stderr capture and the output-limit verdict."""
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from problems.models import Problem, TestCase as ProblemTestCase
from submissions.models import Submission
from submissions.sandbox import run_capture_bounded

_PY = sys.executable


class RunCaptureBoundedTests(TestCase):
    def test_small_output_is_complete_and_unflagged(self):
        r = run_capture_bounded(
            [_PY, "-c", "import sys; sys.stdout.write('ok'); sys.stderr.write('e')"],
            None, 10, 1024,
        )
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "ok")
        self.assertEqual(r.stderr, "e")
        self.assertFalse(r.output_truncated)

    def test_binary_mode_returns_bytes(self):
        r = run_capture_bounded(
            [_PY, "-c", "import sys; sys.stdout.buffer.write(b'bin')"],
            b"", 10, 1024, text=False,
        )
        self.assertEqual(r.stdout, b"bin")
        self.assertIsInstance(r.stdout, bytes)

    def test_stdout_over_cap_is_truncated_and_child_killed(self):
        # 20 MiB producer, 1 MiB cap: must return fast with exactly the
        # cap retained and the overflow flag set (worker RAM bounded).
        code = (
            "import sys\n"
            "buf = b'x' * (1024 * 1024)\n"
            "for _ in range(20):\n"
            "    sys.stdout.buffer.write(buf)\n"
            "    sys.stdout.buffer.flush()\n"
        )
        r = run_capture_bounded([_PY, "-c", code], None, 30, 1024 * 1024)
        self.assertTrue(r.output_truncated)
        self.assertEqual(len(r.stdout), 1024 * 1024)

    def test_stderr_over_cap_is_truncated(self):
        code = (
            "import sys\n"
            "buf = b'y' * 4096\n"
            "for _ in range(1000):\n"
            "    sys.stderr.buffer.write(buf)\n"
            "    sys.stderr.buffer.flush()\n"
        )
        r = run_capture_bounded([_PY, "-c", code], None, 30, 65536)
        self.assertTrue(r.output_truncated)
        self.assertEqual(len(r.stderr), 65536)

    def test_timeout_still_raises(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run_capture_bounded(
                [_PY, "-c", "import time; time.sleep(5)"], None, 0.5, 1024
            )

    def test_stdin_is_piped_through(self):
        code = "import sys; sys.stdout.write(sys.stdin.read())"
        r = run_capture_bounded(
            [_PY, "-c", code], "echo-123", 10, 1024
        )
        self.assertEqual(r.stdout, "echo-123")


class OutputLimitVerdictTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(
            username="output-limit-user", password="safe-test-password")
        self.problem = Problem.objects.create(
            title="Output limit verdict", description="",
            input_format="", output_format="",
            time_limit=1000, memory_limit=256, created_by=user)
        ProblemTestCase.objects.create(
            problem=self.problem, input_data="", expected_output="")
        self.submission = Submission.objects.create(
            problem=self.problem, user=user,
            language="Python", code="print('unused')")

    def test_truncated_run_is_runtime_error_even_at_limit(self):
        from submissions.judge import judge_submission

        result = SimpleNamespace(
            returncode=0, stdout="", stderr="", output_truncated=True)
        with patch("submissions.judge.JudgeContainer") as container_class:
            container = container_class.return_value.__enter__.return_value
            container.exec.return_value = result
            with patch(
                "submissions.judge.SandboxRunner._parse_time_stderr",
                return_value=(1000, 0),
            ):
                judge_submission(self.submission.id)

        self.submission.refresh_from_db()
        case_result = self.submission.test_results.get()
        self.assertEqual(self.submission.status, "Runtime Error")
        self.assertEqual(case_result.status, "Runtime Error")
        self.assertIn("Output limit exceeded", case_result.error_message)
