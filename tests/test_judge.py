from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


class CompileCppInteractiveTests(TestCase):
    """The interactive branch must link the shipped stub into ``user``.

    Contestants write ``#include "sphinx.h"`` and expect the stub to bring
    ``main`` and the interaction helpers along; a submission that includes
    the stub itself must not have it linked a second time. ``_run`` is
    mocked, so only the g++ command lists are inspected (no docker).
    """

    FILES = [
        {'name': 'graders/grader_config.json', 'content': '{}'},
        {
            'name': 'graders/sphinx.h',
            'content': 'std::vector<int> find_colours(int N);\n',
        },
        {
            'name': 'graders/stub.cpp',
            'content': '#include "sphinx.h"\nint main() { return 0; }\n',
        },
        {
            'name': 'graders/manager.cpp',
            'content': 'int main() { return 0; }\n',
        },
    ]

    def _runner(self, tmpdir):
        from submissions.judge import SandboxRunner

        runner = SandboxRunner(
            work_dir=str(tmpdir), time_limit_ms=1500, memory_limit_mb=256,
            image='oj-cpp:latest',
        )
        runner._container = MagicMock()
        runner._global_timeout_sec = 5
        calls = []

        def fake_run(command, timeout, stdin=None, is_compile=False):
            calls.append(list(command))
            return SimpleNamespace(returncode=0, stdout='', stderr='')

        runner._run = fake_run
        patcher = patch('submissions.judge._pch_include_flags', return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        return runner, calls

    def test_stub_is_linked_into_user(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            runner, calls = self._runner(tmp)
            manager, user, err = runner.compile_cpp_interactive(
                '#include "sphinx.h"\nint find_colours(int N) { return N; }\n',
                self.FILES,
            )

            self.assertIsNone(err, f'unexpected compile error: {err}')
            self.assertEqual((manager, user), ('./manager', './user'))
            self.assertTrue((Path(tmp) / 'submission.cpp').exists())
            # The stub is written but compiled into the user binary rather
            # than linked on its own: manager.cpp, submission.cpp, stub.cpp.
            compile_cmds = [c for c in calls if '-c' in c]
            self.assertEqual(len(compile_cmds), 3)
            compiled_srcs = [c[c.index('-c') + 1] for c in compile_cmds]
            self.assertIn('graders/stub.cpp', compiled_srcs)
            self.assertIn('submission.cpp', compiled_srcs)
            # ...it is linked into the user binary together with the submission.
            link = calls[-1]
            self.assertEqual(link[-1], 'user')
            self.assertIn('user_submission.o', link)
            self.assertIn('user_stub.o', link)
            # The manager is built only from manager.cpp.
            self.assertIn('manager_manager.o', calls[1])
            self.assertNotIn('user_stub.o', calls[1])

    def test_stub_already_included_is_not_linked_twice(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            runner, calls = self._runner(tmp)
            _, _, err = runner.compile_cpp_interactive(
                '#include "stub.cpp"\nint find_colours(int N) { return N; }\n',
                self.FILES,
            )

        self.assertIsNone(err, f'unexpected compile error: {err}')
        compile_cmds = [c for c in calls if '-c' in c]
        # manager.cpp + submission.cpp only: the stub is already a textual
        # #include of the submission.
        self.assertEqual(len(compile_cmds), 2)
        link = calls[-1]
        self.assertIn('user_submission.o', link)
        self.assertNotIn('user_stub.o', link)

    def test_missing_manager_is_reported(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = self._runner(tmp)
            _, _, err = runner.compile_cpp_interactive(
                '#include "sphinx.h"\n',
                [f for f in self.FILES if 'manager' not in f['name']],
            )

        self.assertIn('no manager source', err)

