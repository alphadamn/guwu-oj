"""Database-free judging core (Phase 3).

``judge_spec`` runs compile + per-case execution for a plain data bundle
and returns a JSON-serialisable outcome. It performs ZERO ORM / cache
access, so a DB-less judge worker (no PostgreSQL credentials at all) can
execute it: test inputs/expected outputs arrive in the claim response from
the Django internal API, and the outcome goes onto the ``judge:result``
queue for the web-side consumer to persist with the claim fence.

The spec object is a duck-typed mapping with the fields the legacy
``judge_submission`` read off ORM rows::

    {
      "submission_id", "language", "code", "user_id",
      "time_limit_ms", "memory_limit_mb",
      "cases": [{"index": 1, "input": "...", "expected": "..."}],
    }

Outcome shape::

    {"verdict": "Accepted|Wrong Answer|...|Compile Error",
     "runtime_ms": int, "memory_kb": int,
     "cases": [{"index", "status", "runtime_ms",
                "actual_output", "error_message"}]}

Infrastructure failures (Docker unavailable) propagate as
:class:`submissions.sandbox.DockerNotAvailableError`; the worker turns
those into an infra envelope instead of a terminal verdict.
"""

from __future__ import annotations

import logging
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from . import container_pool
from .judge import (
    JUDGED_LANGUAGES,
    LANG_IMAGE,
    SandboxRunner,
    _case_status_from_error,
)

logger = logging.getLogger(__name__)


class JudgeSpecError(Exception):
    """Permanent, infrastructure-independent spec problem (bad language)."""


def _submission_view(spec):
    """Object exposing the attributes compile branches read."""
    return SimpleNamespace(
        id=spec['submission_id'],
        language=spec['language'],
        code=spec['code'],
        user_id=spec.get('user_id', 0),
    )


def _case_views(cases):
    return [
        SimpleNamespace(
            case_index=c['index'],
            input_data=c.get('input', ''),
            expected_output=c.get('expected', ''),
        )
        for c in cases
    ]


def judge_spec(spec, check_alive=None, global_timeout_sec=None):
    """Compile and run one submission against its cases; return outcome."""
    language = spec['language']
    if language not in JUDGED_LANGUAGES:
        raise JudgeSpecError(f'unsupported language {language}')

    cases = spec.get('cases') or []
    if not cases:
        raise JudgeSpecError('no test cases in claim bundle')

    submission = _submission_view(spec)
    tcs = _case_views(cases)
    image = LANG_IMAGE.get(language, 'oj-judge:latest')
    time_limit_ms = int(spec['time_limit_ms'])
    memory_limit_mb = int(spec['memory_limit_mb'])

    pool_handle = container_pool.acquire(
        image, memory_mb=max(memory_limit_mb, 512)
    )
    exec_workdir = None
    try:
        if pool_handle is not None:
            token = secrets.token_hex(8)
            work_dir = f"{pool_handle.host_root.rstrip('/')}/{token}"
            Path(work_dir).mkdir(mode=0o750, parents=True, exist_ok=False)
            exec_workdir = f'/sandbox/{token}'
        else:
            work_dir = tempfile.mkdtemp(prefix='oj_judge_')
    except BaseException:
        container_pool.release(pool_handle, force_destroy=True)
        raise

    max_runtime = 0
    max_memory_kb = 0
    case_outcomes = []
    runner = None
    try:
        runner_kwargs = dict(
            time_limit_ms=time_limit_ms,
            memory_limit_mb=memory_limit_mb,
            image=image,
            pool_handle=pool_handle,
            exec_workdir=exec_workdir,
        )
        if global_timeout_sec is not None:
            runner_kwargs['global_timeout_sec'] = global_timeout_sec
        runner = SandboxRunner(work_dir, **runner_kwargs)

        with runner:
            run_fn = _compile(runner, submission, work_dir)
            if run_fn is None:
                # Compiler diagnostic already encoded by _compile.
                return runner._compile_outcome

            for tc in tcs:
                if check_alive is not None:
                    check_alive()
                runner.last_memory_kb = None
                stdout, elapsed_ms, error = run_fn(tc.input_data)
                actual = stdout if stdout is not None else ''
                expected = tc.expected_output

                if elapsed_ms:
                    max_runtime = max(max_runtime, elapsed_ms)
                if runner.last_memory_kb:
                    max_memory_kb = max(max_memory_kb, runner.last_memory_kb)

                if (
                    elapsed_ms
                    and elapsed_ms >= time_limit_ms
                    and isinstance(error, tuple)
                    and error[0] == 'Runtime Error'
                ):
                    error = 'Time Limit Exceeded'

                parsed = _case_status_from_error(error, actual, expected)
                if isinstance(parsed, tuple):
                    case_status, error_msg = parsed
                    actual = actual or error_msg
                else:
                    case_status = parsed
                    error_msg = ''

                case_outcomes.append({
                    'index': tc.case_index,
                    'status': case_status,
                    'runtime_ms': elapsed_ms,
                    'actual_output': actual,
                    'error_message': error_msg,
                })
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    verdict = 'Accepted'
    for co in case_outcomes:
        if co['status'] != 'Accepted':
            verdict = co['status']
            break
    return _finalize_outcome(
        case_outcomes, max_runtime, max_memory_kb, verdict_override=verdict,
    )


def _compile(runner, submission, work_dir):
    """Return a stdin -> (stdout, elapsed_ms, error) callable, or None.

    None means compilation failed; the Compile Error outcome is stashed on
    ``runner._compile_outcome`` for the caller to return.
    """
    language = submission.language
    code = submission.code

    def compile_failed(message):
        from .judge import truncate_text
        runner._compile_outcome = {
            'verdict': 'Compile Error',
            'runtime_ms': 0,
            'memory_kb': 0,
            'cases': [{
                'index': 1,
                'status': 'Skipped',
                'runtime_ms': None,
                'actual_output': truncate_text(message),
                'error_message': truncate_text(message, 2000),
            }],
        }
        return None

    if language == 'C++':
        exe, err = runner.compile_cpp(code)
        if err:
            return compile_failed(err)
        return lambda stdin: runner.run_executable([exe], stdin)
    if language == 'C':
        exe, err = runner.compile_c(code)
        if err:
            return compile_failed(err)
        return lambda stdin: runner.run_executable([exe], stdin)
    if language == 'Rust':
        exe, err = runner.compile_rust(code)
        if err:
            return compile_failed(err)
        return lambda stdin: runner.run_executable([exe], stdin)
    if language == 'Golang':
        exe, err = runner.compile_golang(code)
        if err:
            return compile_failed(err)
        return lambda stdin: runner.run_executable([exe], stdin)
    if language == 'Assembly':
        exe, err = runner.compile_assembly(code)
        if err:
            return compile_failed(err)
        return lambda stdin: runner.run_executable([exe], stdin)
    if language == 'Java':
        class_name, err = runner.compile_java(code)
        if err:
            return compile_failed(err)
        return lambda stdin: runner.run_executable(['java', class_name], stdin)
    if language == 'Kotlin':
        _, err = runner.compile_kotlin(code)
        if err:
            return compile_failed(err)
        return lambda stdin: runner.run_executable(
            ['java', '-jar', 'main.jar'], stdin,
        )
    if language == 'Python':
        filename = f"{submission.user_id}_{int(time.time() * 1000)}.py"
        Path(work_dir, filename).write_text(code, encoding='utf-8')
        return lambda stdin: runner.run_executable(['python3', filename], stdin)
    if language == 'JavaScript':
        filename = f"{submission.user_id}_{int(time.time() * 1000)}.js"
        Path(work_dir, filename).write_text(code, encoding='utf-8')
        return lambda stdin: runner.run_executable(['node', filename], stdin)
    if language == 'Ruby':
        filename = f"{submission.user_id}_{int(time.time() * 1000)}.rb"
        Path(work_dir, filename).write_text(code, encoding='utf-8')
        return lambda stdin: runner.run_executable(['ruby', filename], stdin)
    raise JudgeSpecError(f'no compile step for language {language}')


def _finalize_outcome(cases, max_runtime, max_memory_kb,
                      verdict_override=None):
    """Normalise/truncate the outcome for the result queue."""
    from .judge import truncate_text

    normalised = []
    verdict = verdict_override or 'Accepted'
    for co in cases:
        if isinstance(co, dict) and 'verdict' in co and 'cases' in co:
            # Compile-error envelope passed through directly.
            return co
        status = co['status']
        if status != 'Accepted' and verdict_override is None:
            verdict = status
        normalised.append({
            'index': co['index'],
            'status': status,
            'runtime_ms': co.get('runtime_ms'),
            'actual_output': truncate_text(co.get('actual_output', '')),
            'error_message': truncate_text(co.get('error_message', ''), 2000),
        })
    return {
        'verdict': verdict,
        'runtime_ms': max_runtime or 0,
        'memory_kb': max_memory_kb or 0,
        'cases': normalised,
    }
