import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from django.utils.crypto import get_random_string

from .models import Submission, SubmissionTestResult
from .sandbox import (
    DockerNotAvailableError,
    exit_indicates_memory_limit,
    run_in_container,
)

JUDGED_LANGUAGES = {'C++', 'Python', 'Java'}
COMPILE_TIMEOUT_SEC = 30
MAX_STORED_OUTPUT_LEN = 4000


def truncate_text(text, limit=MAX_STORED_OUTPUT_LEN):
    if not text:
        return ''
    if len(text) <= limit:
        return text
    return text[:limit] + '\n... (输出已截断)'


def normalize_output(text):
    if text is None:
        return ''
    return text.replace('\r\n', '\n').replace('\r', '\n').rstrip()


def outputs_match(actual, expected):
    return normalize_output(actual) == normalize_output(expected)


def extract_java_class_name(code):
    match = re.search(r'public\s+class\s+(\w+)', code)
    if match:
        return match.group(1)
    match = re.search(r'class\s+(\w+)', code)
    if match:
        return match.group(1)
    return 'Main'


class SandboxRunner:
    """Compile and run submissions inside a Docker container (--network none)."""

    def __init__(self, work_dir, time_limit_ms, memory_limit_mb):
        self.work_dir = work_dir
        self.time_limit_sec = max(time_limit_ms / 1000.0, 0.1)
        self.memory_limit_mb = max(int(memory_limit_mb), 32)

    def _run(self, command, timeout_sec, stdin=None):
        return run_in_container(
            self.work_dir,
            command,
            timeout_sec,
            stdin=stdin,
            memory_mb=self.memory_limit_mb,
        )

    def compile_cpp(self, code):
        src = Path(self.work_dir) / 'main.cpp'
        src.write_text(code, encoding='utf-8')
        try:
            result = self._run(
                ['g++', '-std=c++17', '-O2', '-o', 'main', 'main.cpp'],
                COMPILE_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            return None, 'Compile timeout'
        if result.returncode != 0:
            return None, (result.stderr or result.stdout or 'Compilation failed').strip()
        return './main', None

    def compile_java(self, code):
        class_name = extract_java_class_name(code)
        src = Path(self.work_dir) / f'{class_name}.java'
        src.write_text(code, encoding='utf-8')
        try:
            result = self._run(['javac', f'{class_name}.java'], COMPILE_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            return None, 'Compile timeout'
        if result.returncode != 0:
            return None, (result.stderr or result.stdout or 'Compilation failed').strip()
        return class_name, None

    def run_executable(self, cmd, stdin_data):
        start = time.perf_counter()
        try:
            result = self._run(cmd, self.time_limit_sec, stdin=stdin_data)
        except subprocess.TimeoutExpired:
            return None, None, 'Time Limit Exceeded'
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        if exit_indicates_memory_limit(result.returncode):
            return None, elapsed_ms, 'Memory Limit Exceeded'

        if result.returncode != 0:
            err = (result.stderr or result.stdout or 'Runtime error').strip()
            return None, elapsed_ms, ('Runtime Error', err)
        return result.stdout, elapsed_ms, None

    def run_python(self, code, stdin_data):
        filename = get_random_string(10) + '.py'
        src = Path(self.work_dir) / filename
        src.write_text(code, encoding='utf-8')
        return self.run_executable(['python3', filename], stdin_data)

    def run_cpp(self, executable, stdin_data):
        return self.run_executable([executable], stdin_data)

    def run_java(self, class_name, stdin_data):
        return self.run_executable(['java', class_name], stdin_data)


def save_case_result(submission, tc, case_index, status, runtime, actual, expected, error_message=''):
    SubmissionTestResult.objects.create(
        submission=submission,
        test_case=tc,
        case_index=case_index,
        status=status,
        runtime=runtime,
        actual_output=truncate_text(actual),
        # Never persist hidden expected output to avoid leaking judge data.
        expected_output='',
        error_message=truncate_text(error_message, 2000),
    )


def finalize_submission(submission, case_results, max_runtime, problem):
    """Set overall status from per-case results (first failure wins)."""
    submission.runtime = max_runtime or None
    for status in case_results:
        if status != 'Accepted':
            submission.status = status
            submission.save(update_fields=['status', 'runtime'])
            return
    submission.status = 'Accepted'
    submission.save(update_fields=['status', 'runtime'])
    submission.user.solved_problems.add(problem)


def _case_status_from_error(error, actual, expected):
    if error == 'Time Limit Exceeded':
        return 'Time Limit Exceeded'
    if error == 'Memory Limit Exceeded':
        return 'Memory Limit Exceeded'
    if isinstance(error, tuple) and error[0] == 'Runtime Error':
        return 'Runtime Error', error[1]
    if not outputs_match(actual, expected):
        return 'Wrong Answer'
    return 'Accepted'


def judge_submission(submission_id):
    submission = Submission.objects.select_related('problem', 'user').get(id=submission_id)
    problem = submission.problem
    SubmissionTestResult.objects.filter(submission=submission).delete()

    if submission.language not in JUDGED_LANGUAGES:
        submission.status = 'Pending'
        submission.save(update_fields=['status'])
        return submission

    test_cases = list(problem.test_cases.all())
    if not test_cases:
        submission.status = 'Pending'
        submission.save(update_fields=['status'])
        return submission

    work_dir = tempfile.mkdtemp(prefix='oj_judge_')
    max_runtime = 0
    case_statuses = []

    try:
        runner = SandboxRunner(work_dir, problem.time_limit, problem.memory_limit)

        if submission.language == 'C++':
            executable, err = runner.compile_cpp(submission.code)
            if err:
                submission.status = 'Compile Error'
                submission.save(update_fields=['status'])
                return submission
            run_fn = lambda stdin: runner.run_cpp(executable, stdin)

        elif submission.language == 'Python':
            run_fn = lambda stdin: runner.run_python(submission.code, stdin)

        elif submission.language == 'Java':
            class_name, err = runner.compile_java(submission.code)
            if err:
                submission.status = 'Compile Error'
                submission.save(update_fields=['status'])
                return submission
            run_fn = lambda stdin: runner.run_java(class_name, stdin)

        else:
            return submission

        for idx, tc in enumerate(test_cases, start=1):
            stdout, elapsed_ms, error = run_fn(tc.input_data)
            actual = stdout if stdout is not None else ''
            expected = tc.expected_output
            case_runtime = elapsed_ms
            error_msg = ''

            if elapsed_ms:
                max_runtime = max(max_runtime, elapsed_ms)

            parsed = _case_status_from_error(error, actual, expected)
            if isinstance(parsed, tuple):
                case_status, error_msg = parsed
                actual = actual or error_msg
            else:
                case_status = parsed

            if case_status == 'Time Limit Exceeded':
                case_runtime = case_runtime or problem.time_limit

            save_case_result(
                submission, tc, idx, case_status, case_runtime,
                actual, expected, error_msg,
            )
            case_statuses.append(case_status)

        finalize_submission(submission, case_statuses, max_runtime, problem)

    except DockerNotAvailableError as exc:
        submission.status = 'Runtime Error'
        submission.save(update_fields=['status'])
        if test_cases:
            save_case_result(
                submission, test_cases[0], 1, 'Runtime Error', None,
                '', test_cases[0].expected_output, str(exc),
            )

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    return submission
