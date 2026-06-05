import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import shlex
from pathlib import Path

from django.utils.crypto import get_random_string

from .models import Submission, SubmissionTestResult
from .sandbox import (
    DockerNotAvailableError,
    exit_indicates_memory_limit,
    run_commands_in_container,
    run_in_container,
)

JUDGED_LANGUAGES = {'C++', 'Python', 'Java', 'C', 'Assembly'}
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

    def _run(self, command, timeout_sec, stdin=None, is_compile=False):
        return run_in_container(
            self.work_dir,
            command,
            timeout_sec,
            stdin=stdin,
            memory_mb=self.memory_limit_mb,
            is_compile=is_compile,
        )

    def compile_cpp(self, code):
        src = Path(self.work_dir) / 'main.cpp'
        src.write_text(code, encoding='utf-8')
        # Compile and set permissions in a single container session
        # This ensures the binary persists for execution
        try:
            result = run_commands_in_container(
                self.work_dir,
                [
                    ['g++', '-std=c++17', '-O2', '-o', 'main', 'main.cpp'],
                    ['chmod', '+x', 'main'],
                ],
                COMPILE_TIMEOUT_SEC,
                memory_mb=self.memory_limit_mb,
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
            result = self._run(['javac', f'{class_name}.java'], COMPILE_TIMEOUT_SEC, is_compile=True)
        except subprocess.TimeoutExpired:
            return None, 'Compile timeout'
        if result.returncode != 0:
            return None, (result.stderr or result.stdout or 'Compilation failed').strip()
        return class_name, None

    def compile_assembly(self, code):
        src = Path(self.work_dir) / 'main.s'
        src.write_text(code, encoding='utf-8')
        try:
            result = run_commands_in_container(
                self.work_dir,
                [
                    ['as', '-o', 'main.o', 'main.s'],
                    ['ld', '-o', 'main', 'main.o'],
                    ['chmod', '+x', 'main'],
                ],
                COMPILE_TIMEOUT_SEC,
                memory_mb=self.memory_limit_mb,
            )
        except subprocess.TimeoutExpired:
            return None, 'Compile timeout'
        if result.returncode != 0:
            return None, (result.stderr or result.stdout or 'Compilation failed').strip()
        return './main', None

    def run_executable(self, cmd, stdin_data):
        # Execute the command inside the container and measure runtime using the container's own timer to avoid Docker startup overhead.
        # We wrap the command with `/usr/bin/time -f %e` via a bash shell, which prints elapsed seconds to stderr.
        # The command's stdout is captured as usual; the first line of stderr contains the elapsed time.
        try:
            # Build a single string command for bash -c
            cmd_str = ' '.join(shlex.quote(arg) for arg in cmd)
            wrapped_cmd = ['/bin/bash', '-lc', f'time {cmd_str}']
            result = self._run(wrapped_cmd, self.time_limit_sec, stdin=stdin_data, is_compile=False)
            # Parse elapsed time from stderr (first line)
            elapsed_sec = None
            if result.stderr:
                first_line = result.stderr.splitlines()[1].strip()
                # print(first_line)
                try:
                    # elapsed_sec = float(first_line)
                    match = re.search(r'real\s+(\d+)m(\d+(?:\.\d+)?)s', first_line)
                    if match:
                        minutes = int(match.group(1))
                        seconds = float(match.group(2))
                        elapsed_sec = minutes * 60 + seconds
                except ValueError:
                    pass
            elapsed_ms = int(elapsed_sec * 1000) if elapsed_sec is not None else None
        except subprocess.TimeoutExpired:
            return None, None, 'Time Limit Exceeded'

        if exit_indicates_memory_limit(result.returncode):
            return None, elapsed_ms, 'Memory Limit Exceeded'

        if result.returncode != 0:
            err = (result.stderr or result.stdout or 'Runtime error').strip()
            # print(f"Command failed: {cmd}")
            # print(f"Return code: {result.returncode}")
            # print(f"Stderr: {result.stderr}")
            # print(f"Stdout: {result.stdout}")
            # print(f"Error: {err}")
            return None, elapsed_ms, ('Runtime Error', err)
        return result.stdout, elapsed_ms, None

    def run_python(self, code, stdin_data):
        filename = get_random_string(10) + '.py'
        src = Path(self.work_dir) / filename
        src.write_text(code, encoding='utf-8')
        return self.run_executable(['python3', filename], stdin_data)

    def run_cpp(self, executable, stdin_data):
        return self.run_executable([executable], stdin_data)

    def run_cpp_combined(self, code, stdin_data):
        # Compile and execute in the same container session to avoid binary persistence issues
        src = Path(self.work_dir) / 'main.cpp'
        src.write_text(code, encoding='utf-8')
        # start = time.perf_counter()
        try:
            # Compile first
            compile_result = self._run(
                ['g++', '-std=c++17', '-O2', '-o', 'main', 'main.cpp'],
                COMPILE_TIMEOUT_SEC,
                is_compile=True,
            )
            if compile_result.returncode != 0:
                err = (compile_result.stderr or compile_result.stdout or 'Compilation failed').strip()
                return None, 0, ('Runtime Error', err)
            
            # Set execute permission
            self._run(['chmod', '+x', 'main'], 5, is_compile=True)
            
            # Copy binary to host using docker cp to bypass bind mount sync issues
            # Get container ID from the last run (this is tricky, so let's try a different approach)
            # Instead, let's verify the binary exists on the host filesystem
            binary_path = Path(self.work_dir) / 'main'
            if not binary_path.exists():
                print(f"Binary NOT found on host after compilation: {binary_path}")
                return None, 0, ('Runtime Error', 'Binary not found after compilation')
            
            os.chmod(binary_path, 0o755)
            print(f"Binary exists on host: {binary_path}")
            
            # Execute using absolute path
            # start = time.perf_counter()
            try:
                cmd_str = ' '.join(shlex.quote(arg) for arg in ['/sandbox/main'])
                wrapped_cmd = ['/bin/bash', '-lc', f'time {cmd_str}']
                result = self._run(wrapped_cmd, self.time_limit_sec, stdin=stdin_data, is_compile=False)
                elapsed_sec = None
                if result.stderr:
                    first_line = result.stderr.splitlines()[1].strip()
                    # print(first_line)
                    try:
                        # elapsed_sec = float(first_line)
                        match = re.search(r'real\s+(\d+)m(\d+(?:\.\d+)?)s', first_line)
                        if match:
                            minutes = int(match.group(1))
                            seconds = float(match.group(2))
                            elapsed_sec = minutes * 60 + seconds
                    except ValueError:
                        pass
                elapsed_ms = int(elapsed_sec * 1000) if elapsed_sec is not None else None
            except subprocess.TimeoutExpired:
                return None, None, 'Time Limit Exceeded'

            if exit_indicates_memory_limit(result.returncode):
                return None, elapsed_ms, 'Memory Limit Exceeded'

            if result.returncode != 0:
                err = (result.stderr or result.stdout or 'Runtime error').strip()
                # print(f"Command failed: {wrapped_cmd}")
                # print(f"Return code: {result.returncode}")
                # print(f"Stderr: {result.stderr}")
                # print(f"Stdout: {result.stdout}")
                # print(f"Error: {err}")
                return None, elapsed_ms, ('Runtime Error', err)
            return result.stdout, elapsed_ms, None
        except subprocess.TimeoutExpired:
            return None, None, 'Time Limit Exceeded'
        # elapsed_ms = int((time.perf_counter() - start) * 1000)

        if exit_indicates_memory_limit(result.returncode):
            return None, elapsed_ms, 'Memory Limit Exceeded'

        if result.returncode != 0:
            err = (result.stderr or result.stdout or 'Runtime error').strip()
            print(f"Command failed")
            print(f"Return code: {result.returncode}")
            print(f"Stderr: {result.stderr}")
            print(f"Stdout: {result.stdout}")
            print(f"Error: {err}")
            return None, elapsed_ms, ('Runtime Error', err)
        return result.stdout, elapsed_ms, None

    def run_c_combined(self, code, stdin_data):
        # Compile and execute in the same container session to avoid binary persistence issues
        src = Path(self.work_dir) / 'main.c'
        src.write_text(code, encoding='utf-8')
        # start = time.perf_counter()
        try:
            # Compile first
            compile_result = self._run(
                ['gcc', '-O2', '-o', 'main', 'main.c'],
                COMPILE_TIMEOUT_SEC,
                is_compile=True,
            )
            if compile_result.returncode != 0:
                err = (compile_result.stderr or compile_result.stdout or 'Compilation failed').strip()
                return None, 0, ('Runtime Error', err)

            # Set execute permission
            self._run(['chmod', '+x', 'main'], 5, is_compile=True)

            # Copy binary to host using docker cp to bypass bind mount sync issues
            # Get container ID from the last run (this is tricky, so let's try a different approach)
            # Instead, let's verify the binary exists on the host filesystem
            binary_path = Path(self.work_dir) / 'main'
            if not binary_path.exists():
                print(f"Binary NOT found on host after compilation: {binary_path}")
                return None, 0, ('Runtime Error', 'Binary not found after compilation')

            os.chmod(binary_path, 0o755)
            print(f"Binary exists on host: {binary_path}")

            # Execute using absolute path
            # start = time.perf_counter()
            try:
                cmd_str = ' '.join(shlex.quote(arg) for arg in ['/sandbox/main'])
                wrapped_cmd = ['/bin/bash', '-lc', f'time {cmd_str}']
                result = self._run(wrapped_cmd, self.time_limit_sec, stdin=stdin_data, is_compile=False)
                elapsed_sec = None
                if result.stderr:
                    first_line = result.stderr.splitlines()[1].strip()
                    # print(first_line)
                    try:
                        # elapsed_sec = float(first_line)
                        match = re.search(r'real\s+(\d+)m(\d+(?:\.\d+)?)s', first_line)
                        if match:
                            minutes = int(match.group(1))
                            seconds = float(match.group(2))
                            elapsed_sec = minutes * 60 + seconds
                    except ValueError:
                        pass
                try:
                    elapsed_ms = int(elapsed_sec * 1000)
                except Exception:
                    elapsed_sec = None
            except subprocess.TimeoutExpired:
                return None, None, 'Time Limit Exceeded'

            if exit_indicates_memory_limit(result.returncode):
                return None, elapsed_ms, 'Memory Limit Exceeded'

            if result.returncode != 0:
                err = (result.stderr or result.stdout or 'Runtime error').strip()
                # print(f"Command failed: {wrapped_cmd}")
                # print(f"Return code: {result.returncode}")
                # print(f"Stderr: {result.stderr}")
                # print(f"Stdout: {result.stdout}")
                # print(f"Error: {err}")
                return None, elapsed_ms, ('Runtime Error', err)
            return result.stdout, elapsed_ms, None
        except subprocess.TimeoutExpired:
            return None, None, 'Time Limit Exceeded'

    def run_assembly_combined(self, code, stdin_data):
        # Compile and execute assembly in the same container session to avoid binary persistence issues
        src = Path(self.work_dir) / 'main.s'
        src.write_text(code, encoding='utf-8')
        try:
            # Assemble first
            compile_result = self._run(
                ['as', '-o', 'main.o', 'main.s'],
                COMPILE_TIMEOUT_SEC,
                is_compile=True,
            )
            if compile_result.returncode != 0:
                err = (compile_result.stderr or compile_result.stdout or 'Compilation failed').strip()
                return None, 0, ('Runtime Error', err)

            # Link
            link_result = self._run(
                ['ld', '-o', 'main', 'main.o'],
                COMPILE_TIMEOUT_SEC,
                is_compile=True,
            )
            if link_result.returncode != 0:
                err = (link_result.stderr or link_result.stdout or 'Link failed').strip()
                return None, 0, ('Runtime Error', err)

            # Set execute permission
            self._run(['chmod', '+x', 'main'], 5, is_compile=True)

            # Verify the binary exists on the host filesystem
            binary_path = Path(self.work_dir) / 'main'
            if not binary_path.exists():
                print(f"Binary NOT found on host after compilation: {binary_path}")
                return None, 0, ('Runtime Error', 'Binary not found after compilation')

            os.chmod(binary_path, 0o755)
            print(f"Binary exists on host: {binary_path}")

            # Execute using absolute path
            try:
                cmd_str = ' '.join(shlex.quote(arg) for arg in ['/sandbox/main'])
                wrapped_cmd = ['/bin/bash', '-lc', f'time {cmd_str}']
                result = self._run(wrapped_cmd, self.time_limit_sec, stdin=stdin_data, is_compile=False)
                elapsed_sec = None
                if result.stderr:
                    first_line = result.stderr.splitlines()[1].strip()
                    try:
                        match = re.search(r'real\s+(\d+)m(\d+(?:\.\d+)?)s', first_line)
                        if match:
                            minutes = int(match.group(1))
                            seconds = float(match.group(2))
                            elapsed_sec = minutes * 60 + seconds
                    except ValueError:
                        pass
                try:
                    elapsed_ms = int(elapsed_sec * 1000)
                except Exception:
                    elapsed_sec = None
            except subprocess.TimeoutExpired:
                return None, None, 'Time Limit Exceeded'

            if exit_indicates_memory_limit(result.returncode):
                return None, elapsed_ms, 'Memory Limit Exceeded'

            if result.returncode != 0:
                err = (result.stderr or result.stdout or 'Runtime error').strip()
                return None, elapsed_ms, ('Runtime Error', err)
            return result.stdout, elapsed_ms, None
        except subprocess.TimeoutExpired:
            return None, None, 'Time Limit Exceeded'

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
    submission.runtime = max_runtime or 0
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

        # Warm‑up: compile and run a minimal hello‑world program in the submission's language.
        # This pre‑initialises the Docker container without affecting scoring.
        try:
            if submission.language == 'C++':
                exe, err = runner.compile_cpp('int main(){return 0;}')
                if exe:
                    runner.run_executable([exe], None)
            elif submission.language == 'C':
                # Create a simple C source file
                src = Path(work_dir) / 'hello.c'
                src.write_text('int main(){return 0;}', encoding='utf-8')
                compile_res = runner._run(['gcc', '-O2', '-o', 'hello', 'hello.c'], COMPILE_TIMEOUT_SEC, is_compile=True)
                if compile_res.returncode == 0:
                    runner.run_executable(['./hello'], None)
            elif submission.language == 'Python':
                src = Path(work_dir) / 'hello.py'
                src.write_text('pass', encoding='utf-8')
                runner.run_executable(['python3', src.name], None)
            elif submission.language == 'Java':
                class_name = 'Hello'
                src = Path(work_dir) / f'{class_name}.java'
                src.write_text('public class Hello { public static void main(String[] args) {} }', encoding='utf-8')
                compiled_name, err = runner.compile_java(src.read_text())
                if compiled_name:
                    runner.run_executable(['java', compiled_name], None)
            elif submission.language == 'Assembly':
                src = Path(work_dir) / 'hello.s'
                src.write_text('.section .text\n.global _start\n_start:\n    mov x0, #0\n    mov x8, #93\n    svc #0', encoding='utf-8')
                compile_res = runner._run(['as', '-o', 'hello.o', 'hello.s'], COMPILE_TIMEOUT_SEC, is_compile=True)
                if compile_res.returncode == 0:
                    link_res = runner._run(['ld', '-o', 'hello', 'hello.o'], COMPILE_TIMEOUT_SEC, is_compile=True)
                    if link_res.returncode == 0:
                        runner.run_executable(['./hello'], None)
        except Exception:
            # Ignore any warm‑up failures; real test cases will surface problems.
            pass

        if submission.language == 'C++':
            # For C++, compile and execute in the same container session to avoid binary persistence issues
            src = Path(work_dir) / 'main.cpp'
            src.write_text(submission.code, encoding='utf-8')
            run_fn = lambda stdin: runner.run_cpp_combined(submission.code, stdin)

        elif submission.language == 'C':
            src = Path(work_dir) / 'main.c'
            src.write_text(submission.code, encoding='utf-8')
            run_fn = lambda stdin: runner.run_c_combined(submission.code, stdin)

        elif submission.language == 'Python':
            run_fn = lambda stdin: runner.run_python(submission.code, stdin)

        elif submission.language == 'Java':
            class_name, err = runner.compile_java(submission.code)
            if err:
                submission.status = 'Compile Error'
                submission.save(update_fields=['status'])
                return submission
            run_fn = lambda stdin: runner.run_java(class_name, stdin)

        elif submission.language == 'Assembly':
            src = Path(work_dir) / 'main.s'
            src.write_text(submission.code, encoding='utf-8')
            run_fn = lambda stdin: runner.run_assembly_combined(submission.code, stdin)

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
