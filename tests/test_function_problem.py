"""Tests for IOI-style function-problem support.

Covers:
* Problem model: ``problem_type`` default + ``function_files_parsed``
  property (robust against empty / malformed JSON).
* Fingerprint: digest + shape change when ``function_files`` is edited
  (so the worker cache invalidates on a grader change).
* Claim bundle: ``/internal/judge/claim/`` returns ``problem_type`` and
  ``function_files`` so the DB-less worker can write them to its work dir.
* ``SandboxRunner.compile_cpp``: function branch writes
  ``submission.cpp`` + each grader file, and dispatches a separate g++
  command per translation unit (so ccache can cache each).
* Submit endpoint: non-C++ submissions to function problems are rejected
  with the same ``messages.error`` + re-render pattern used for login
  failures and captcha mistakes.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from problems.fingerprint import _digest, _shape, invalidate_test_data_fingerprint
from problems.forms import (
    parse_function_files_from_post,
    validate_function_files,
)
from problems.models import Problem, TestCase as ProblemTestCase


User = get_user_model()


def _make_problem(problem_type='standard', function_files='[]', **kwargs):
    # The custom User model enforces a unique email; pass distinct emails so
    # multiple _make_problem calls in the same transaction don't collide.
    user, _ = User.objects.get_or_create(
        username='tester',
        defaults={'email': 'tester@example.com'},
    )
    defaults = dict(
        title='t', description='d', input_format='', output_format='',
        difficulty='普及', time_limit=1000, memory_limit=256,
        tags='', created_by=user, is_public=True,
        problem_type=problem_type, function_files=function_files,
    )
    defaults.update(kwargs)
    return Problem.objects.create(**defaults)


class ProblemModelTests(TestCase):
    def test_default_problem_type_is_standard(self):
        p = _make_problem()
        self.assertEqual(p.problem_type, 'standard')

    def test_function_files_default_empty_json(self):
        p = _make_problem()
        self.assertEqual(p.function_files, '[]')

    def test_function_files_parsed_empty_for_blank(self):
        p = _make_problem(function_files='')
        self.assertEqual(p.function_files_parsed, [])

    def test_function_files_parsed_for_malformed_json(self):
        p = _make_problem(function_files='not json {')
        self.assertEqual(p.function_files_parsed, [])

    def test_function_files_parsed_skips_non_dict_entries(self):
        payload = json.dumps([
            {'name': 'problem.h', 'content': 'x'},
            'bad-entry',  # not a dict
            {'name': '', 'content': 'no-name'},  # empty name dropped
            {'name': 'grader.cpp', 'content': 'int main(){}'},
        ])
        p = _make_problem(problem_type='function', function_files=payload)
        names = [f['name'] for f in p.function_files_parsed]
        self.assertEqual(names, ['problem.h', 'grader.cpp'])

    def test_function_files_parsed_returns_dicts_with_name_and_content(self):
        payload = json.dumps([{'name': 'a.h', 'content': 'x'}])
        p = _make_problem(problem_type='function', function_files=payload)
        files = p.function_files_parsed
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0], {'name': 'a.h', 'content': 'x'})


class FingerprintTests(TestCase):
    def setUp(self):
        # One shared test case so the fingerprint is non-empty.
        self.problem = _make_problem(
            problem_type='function',
            function_files=json.dumps(
                [{'name': 'grader.cpp', 'content': 'int main(){}'}]
            ),
        )
        ProblemTestCase.objects.create(
            problem=self.problem, input_data='1', expected_output='2',
            order=0,
        )
        invalidate_test_data_fingerprint(self.problem.id)

    def test_digest_includes_function_files(self):
        before = _digest(self.problem.id)
        # Edit the grader — fingerprint MUST change so a worker that cached
        # the previous test-data bundle re-downloads.
        self.problem.function_files = json.dumps(
            [{'name': 'grader.cpp', 'content': 'int main(){ return 1; }'}]
        )
        self.problem.save()
        invalidate_test_data_fingerprint(self.problem.id)
        after = _digest(self.problem.id)
        self.assertNotEqual(before, after)

    def test_shape_includes_function_files(self):
        before = _shape(self.problem.id)
        self.problem.function_files = json.dumps(
            [{'name': 'grader.cpp', 'content': 'changed'}]
        )
        self.problem.save()
        invalidate_test_data_fingerprint(self.problem.id)
        after = _shape(self.problem.id)
        self.assertNotEqual(before, after)
        # The shape tuple has four elements: count, max_id, order_sum, files_hash.
        self.assertEqual(len(before), 4)
        self.assertEqual(len(after), 4)
        # First three (TestCase aggregates) are unchanged.
        self.assertEqual(before[:3], after[:3])
        # The files hash moved.
        self.assertNotEqual(before[3], after[3])


class ClaimBundleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username='claimer')
        self.problem = _make_problem(
            problem_type='function',
            function_files=json.dumps([
                {'name': 'problem.h', 'content': 'int add(int,int);'},
                {'name': 'grader.cpp', 'content': 'int main(){return 0;}'},
            ]),
            is_public=True,
        )
        ProblemTestCase.objects.create(
            problem=self.problem, input_data='1 2', expected_output='3',
            order=0,
        )
        from submissions.models import Submission
        self.submission = Submission.objects.create(
            problem=self.problem, user=self.user,
            code='int add(int a,int b){return a+b;}',
            language='C++', status='Pending',
        )

    def _post_claim(self):
        from django.test import Client
        from django.conf import settings
        c = Client()
        with patch('submissions.claiming.claim_submission', return_value='tok'):
            resp = c.post(
                '/internal/judge/claim/',
                data=json.dumps({
                    'submission_id': self.submission.id,
                    'worker_id': 'w1',
                }),
                content_type='application/json',
                HTTP_X_JUDGE_TOKEN=getattr(settings, 'JUDGE_INTERNAL_TOKEN', '') or 'x',
            )
        return resp

    def test_claim_returns_problem_type_and_function_files(self):
        resp = self._post_claim()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body['problem_type'], 'function')
        self.assertEqual(len(body['function_files']), 2)
        names = [f['name'] for f in body['function_files']]
        self.assertIn('problem.h', names)
        self.assertIn('grader.cpp', names)

    def test_claim_returns_standard_and_empty_for_standard_problem(self):
        std = _make_problem(
            title='std', problem_type='standard', function_files='[]',
        )
        ProblemTestCase.objects.create(
            problem=std, input_data='', expected_output='', order=0,
        )
        from submissions.models import Submission
        sub = Submission.objects.create(
            problem=std, user=self.user, code='int main(){}',
            language='C++', status='Pending',
        )
        from django.test import Client
        from django.conf import settings
        c = Client()
        with patch('submissions.claiming.claim_submission', return_value='tok'):
            resp = c.post(
                '/internal/judge/claim/',
                data=json.dumps({'submission_id': sub.id, 'worker_id': 'w2'}),
                content_type='application/json',
                HTTP_X_JUDGE_TOKEN=getattr(settings, 'JUDGE_INTERNAL_TOKEN', '') or 'x',
            )
        body = resp.json()
        self.assertEqual(body['problem_type'], 'standard')
        self.assertEqual(body['function_files'], [])


class CompileCppFunctionTests(TestCase):
    """Verify the function branch writes the expected files + g++ commands.

    Mocks ``SandboxRunner._run`` so the test does not need docker; we
    inspect the command lists and the files left in the work dir.
    """

    def _runner_no_pch(self, tmpdir):
        """Build a SandboxRunner with mocked _run + PCH probe stubbed.

        Returns (runner, calls) where calls captures each _run command and
        excludes the PCH probe (which would otherwise add a 4th entry).
        """
        from submissions.judge import SandboxRunner
        runner = SandboxRunner(
            work_dir=str(tmpdir), time_limit_ms=1000, memory_limit_mb=256,
            image='oj-cpp:latest',
        )
        runner._container = MagicMock()
        runner._global_timeout_sec = 5

        calls = []

        class _Res:
            returncode = 0
            stdout = ''
            stderr = ''

        def fake_run(cmd, timeout, stdin=None, is_compile=False):
            calls.append(list(cmd))
            return _Res()

        runner._run = fake_run
        # Stub the module-level helper directly so no probe runs.
        import submissions.judge as judge_mod
        orig = judge_mod._pch_include_flags
        judge_mod._pch_include_flags = lambda r, image: []

        def restore():
            judge_mod._pch_include_flags = orig

        self.addCleanup(restore)
        return runner, calls

    def test_function_branch_writes_files_and_links(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            runner, calls = self._runner_no_pch(tmp)
            grader = (
                '#include "problem.h"\n'
                '#include <iostream>\n'
                'int main(){std::cout<<add(1,2);}\n'
            )
            header = 'int add(int,int);\n'
            user_code = 'int add(int a,int b){return a+b;}\n'
            files = [
                {'name': 'problem.h', 'content': header},
                {'name': 'grader.cpp', 'content': grader},
            ]
            exe, err = runner.compile_cpp(
                user_code, problem_type='function', function_files=files,
            )
            self.assertIsNone(err, f"unexpected compile error: {err}")
            self.assertEqual(exe, './main')
            # User's submission + both grader files written to work_dir.
            self.assertTrue((Path(tmp) / 'submission.cpp').exists())
            self.assertTrue((Path(tmp) / 'problem.h').exists())
            self.assertTrue((Path(tmp) / 'grader.cpp').exists())
            # Two -c compiles + one link = 3 _run calls (PCH probe stubbed).
            self.assertEqual(len(calls), 3)
            # Compile commands hit -c on each .cpp.
            self.assertIn('-c', calls[0])
            self.assertIn('-c', calls[1])
            # Link command is `g++ <objs> -o main`.
            self.assertEqual(calls[2][0], 'g++')
            self.assertIn('-o', calls[2])
            self.assertIn('main', calls[2])

    def test_include_style_grader_drops_submission_from_tu_list(self):
        """A grader that ``#include "submission.cpp"`` must NOT be compiled
        alongside submission.cpp — otherwise the function symbols are defined
        twice and the link step fails."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            runner, calls = self._runner_no_pch(tmp)
            grader = (
                '#include "problem.h"\n'
                '#include "submission.cpp"\n'
                'int main(){return add(1,2);}\n'
            )
            files = [
                {'name': 'problem.h', 'content': 'int add(int,int);'},
                {'name': 'grader.cpp', 'content': grader},
            ]
            exe, err = runner.compile_cpp(
                'int add(int a,int b){return a+b;}',
                problem_type='function', function_files=files,
            )
            self.assertIsNone(err)
            # Only ONE -c call (grader.cpp); submission.cpp is NOT a TU.
            compile_cmds = [c for c in calls if '-c' in c]
            self.assertEqual(len(compile_cmds), 1)
            self.assertIn('grader.cpp', compile_cmds[0])
            self.assertNotIn('submission.cpp', compile_cmds[0])

    def test_standard_path_unchanged_when_function_files_empty(self):
        """problem_type='standard' must take the original single-main.cpp path."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            runner, calls = self._runner_no_pch(tmp)
            exe, err = runner.compile_cpp(
                'int main(){return 0;}', problem_type='standard',
                function_files=[],
            )
            self.assertIsNone(err)
            # main.cpp written (not submission.cpp).
            self.assertTrue((Path(tmp) / 'main.cpp').exists())
            self.assertFalse((Path(tmp) / 'submission.cpp').exists())

    def test_function_branch_rejects_path_traversal(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = self._runner_no_pch(tmp)
            files = [{'name': '../escape.cpp', 'content': 'x'}]
            exe, err = runner.compile_cpp(
                'int main(){}', problem_type='function', function_files=files,
            )
            self.assertIsNone(exe)
            self.assertIn('Illegal file path', err)


class FormsTests(TestCase):
    def test_parse_function_files_skips_path_separators(self):
        from django.http import QueryDict
        q = QueryDict('', mutable=True)
        q.setlist('func_file_name_0', ['ok.h'])
        q.setlist('func_file_content_0', ['h'])
        q.setlist('func_file_name_1', ['bad/path.h'])
        q.setlist('func_file_content_1', ['x'])
        q.setlist('func_file_name_2', ['.dotfile.h'])
        q.setlist('func_file_content_2', ['x'])
        files = parse_function_files_from_post(q)
        self.assertEqual([f['name'] for f in files], ['ok.h'])

    def test_validate_function_files_requires_cpp(self):
        self.assertIsNone(validate_function_files([], 'standard'))
        self.assertIsNotNone(validate_function_files([], 'function'))
        msg = validate_function_files(
            [{'name': 'only.h', 'content': 'x'}], 'function')
        self.assertIn('.cpp', msg)
        self.assertIsNone(validate_function_files(
            [{'name': 'g.cpp', 'content': 'x'}], 'function'))


class SubmitRejectionTests(TestCase):
    """Non-C++ submissions to function problems get a popup (messages.error)."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='sub', password='pw', email='sub@example.com',
        )
        self.problem = _make_problem(
            problem_type='function',
            function_files=json.dumps(
                [{'name': 'g.cpp', 'content': 'int main(){return 0;}'}]
            ),
            is_public=True,
        )
        ProblemTestCase.objects.create(
            problem=self.problem, input_data='', expected_output='',
            order=0,
        )

    def test_python_submission_to_function_problem_rejected(self):
        from django.test import Client
        c = Client()
        c.force_login(self.user)
        resp = c.post(
            f'/submissions/submit/{self.problem.id}/',
            data={'code': 'print(1)', 'language': 'Python'},
        )
        # 200 (re-rendered submit page) — not a redirect to detail.
        self.assertEqual(resp.status_code, 200)
        # The messages framework carries the C++-only popup. The redirect
        # branch returns 302 and never re-renders, so a 200 here already
        # proves the guard fired.
        self.assertEqual(resp.templates[0].name, 'submissions/submit.html')

    def test_cpp_submission_to_function_problem_proceeds(self):
        from django.test import Client
        from submissions.models import Submission
        c = Client()
        c.force_login(self.user)
        with patch('submissions.views.enqueue_judge'):
            resp = c.post(
                f'/submissions/submit/{self.problem.id}/',
                data={'code': 'int add(int a,int b){return a+b;}',
                      'language': 'C++'},
            )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Submission.objects.filter(problem=self.problem).exists())
