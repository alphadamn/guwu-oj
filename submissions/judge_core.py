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
      "total_cases": 26,
      "cases": [{"index": 1, "input": "...", "expected": "..."}],
    }

``cases`` may be empty, in which case ``total_cases`` says how many exist
and the caller passes ``case_loader`` to pull them in batches as judging
proceeds.

Outcome shape::

    {"verdict": "Accepted|Wrong Answer|...|Compile Error",
     "runtime_ms": int, "memory_kb": int,
     "cases": [{"index", "status", "runtime_ms",
                "actual_output", "error_message"}],
     "timings": {"judge_started_at", "compile_done_at", "tests_done_at"}}

``timings`` carries the phase boundaries this worker is the only one to
witness (absolute UTC ISO-8601 strings, so the envelope stays JSON-clean).
The web-side consumer stamps them onto the submission row; a phase that
never ran (no test phase after a Compile Error) is simply absent.

Infrastructure failures (Docker unavailable) propagate as
:class:`submissions.sandbox.DockerNotAvailableError`; the worker turns
those into an infra envelope instead of a terminal verdict.
"""

from __future__ import annotations

import logging
import secrets
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
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
        # Function-style problems: ship the grader/header files alongside
        # the source so compile_cpp can write them to the work dir. Standard
        # problems send problem_type='standard' and an empty list (no-op).
        problem_type=spec.get('problem_type') or 'standard',
        function_files=spec.get('function_files') or [],
        # Interactive problems: which Communication protocol and how many
        # user processes (see problems.Problem.interactive_config).
        interactive_config=spec.get('interactive_config') or {},
    )


def _utcnow_iso() -> str:
    """Phase-boundary timestamp for the result envelope (absolute UTC)."""
    return datetime.now(timezone.utc).isoformat()


def _case_view(case):
    return SimpleNamespace(
        case_index=case['index'],
        input_data=case.get('input', ''),
        expected_output=case.get('expected', ''),
    )


# Test cases pulled per round trip when the claim carried no inline data.
# Small enough that the results panel fills in continuously, large enough
# that per-request overhead stays in the noise.
CASE_FETCH_BATCH = 4


class _CaseFeed:
    """Supplies test cases in order, from inline data or a batched loader.

    A DB-less worker no longer receives test data in the claim response -
    the web side reports only how many cases exist. Cases are therefore
    pulled in batches *while judging is already running*, which keeps
    ``judge_started_at`` independent of the payload size (an unlucky
    problem can carry several hundred megabytes of test data), and each
    case is dropped after use so the whole set never has to fit in memory.

    A background thread does the pulling. Downloading dominates the test
    phase on a fat problem (the judge uplink is the bottleneck), so fetching
    synchronously would idle the CPU after every batch and hand the results
    over in visible stalls. Prefetching keeps one batch in hand, so the
    judging loop drains it while the next one is still on the wire.
    """

    def __init__(self, inline_cases, total, loader, batch=CASE_FETCH_BATCH):
        self._pending = {int(c['index']): c for c in inline_cases}
        self._total = int(total)
        # Inline data (legacy claim) is complete by definition; a loader is
        # only meaningful when it isn't.
        self._loader = None if inline_cases else loader
        self._batch = max(1, int(batch))
        self._offset = len(inline_cases)
        self._cond = threading.Condition()
        self._error = None
        self._exhausted = False
        self._closed = False
        self._thread = None
        if self._loader is not None:
            self._thread = threading.Thread(
                target=self._prefetch, name='oj-case-prefetch', daemon=True,
            )
            self._thread.start()

    @property
    def total(self):
        return self._total

    def _prefetch(self):
        """Pull batches ahead of the judging loop until told to stop."""
        try:
            while True:
                with self._cond:
                    # Hold off when a full batch is already buffered, or when
                    # every case has been handed out.
                    while not self._closed and (
                        len(self._pending) >= self._batch
                        or self._offset >= self._total
                    ):
                        if self._offset >= self._total:
                            self._exhausted = True
                        self._cond.wait()
                    if self._closed:
                        return
                    offset = self._offset
                    # Ask for exactly what is left. A cache that stores whole
                    # ranges only answers a request it can satisfy in full, so
                    # asking for a full batch past the end of the test set
                    # always misses and forces a network round trip -- which
                    # then lands as a stall just before the final cases.
                    want = min(self._batch, self._total - offset)
                rows = self._loader(offset, want)
                with self._cond:
                    if self._closed:
                        return
                    if not rows:
                        self._exhausted = True
                        self._cond.notify_all()
                        return
                    for row in rows:
                        self._pending[int(row['index'])] = row
                    self._offset += len(rows)
                    self._cond.notify_all()
        except BaseException as exc:  # noqa: BLE001 - re-raised to the judge
            with self._cond:
                self._error = exc
                self._cond.notify_all()

    def case(self, index):
        if index < 1 or index > self._total:
            raise JudgeSpecError(f'test case {index} out of range')
        with self._cond:
            while index not in self._pending:
                if self._error is not None:
                    raise self._error
                if self._loader is None:
                    raise JudgeSpecError(f'test case {index} unavailable')
                if self._exhausted:
                    raise JudgeSpecError(
                        f'test data feed ended before case {index}'
                    )
                self._cond.wait()
            case = self._pending.pop(index)
            # A slot just freed: let the prefetcher start the next batch.
            self._cond.notify_all()
        return _case_view(case)

    def close(self):
        """Stop prefetching. Safe to call more than once."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=1)

def judge_spec(spec, check_alive=None, global_timeout_sec=None,
               case_loader=None, on_case_done=None):
    """Compile and run one submission against its cases; return outcome.

    Test data comes either inline (``spec['cases']``, legacy claim) or, when
    only ``spec['total_cases']`` is present, from ``case_loader(offset,
    limit)`` on demand. ``on_case_done(case_dict)`` is invoked after each
    case finishes so a caller can report progress incrementally; it must not
    raise (a failed progress report is never worth losing a verdict).
    """
    language = spec['language']
    if language not in JUDGED_LANGUAGES:
        raise JudgeSpecError(f'unsupported language {language}')

    inline_cases = list(spec.get('cases') or [])
    total_cases = int(spec.get('total_cases') or 0) or len(inline_cases)
    if total_cases <= 0 or (not inline_cases and case_loader is None):
        raise JudgeSpecError('no test cases in claim bundle')

    # Judging work starts here; everything from claimed_at to this point is
    # claim + dispatch overhead on the web side.
    judge_started_at = _utcnow_iso()

    submission = _submission_view(spec)
    feed = _CaseFeed(inline_cases, total_cases, case_loader)
    image = LANG_IMAGE.get(language, 'oj-judge:latest')
    time_limit_ms = int(spec['time_limit_ms'])
    memory_limit_mb = int(spec['memory_limit_mb'])

    pool_handle = container_pool.acquire(
        image, memory_mb=max(memory_limit_mb, 512)
    )
    # The pool has handed over a warm container (or returned None, telling us
    # to fall back to an ephemeral one). Either way the checkout call is done,
    # so the gap from judge_started_at is time spent waiting on the pool.
    container_acquired_at = _utcnow_iso()
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
    compile_done_at = None
    tests_done_at = None
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
            compile_done_at = _utcnow_iso()
            if run_fn is None:
                # Compiler diagnostic already encoded by _compile. No test
                # phase ran, so tests_done_at is deliberately absent.
                outcome = dict(runner._compile_outcome)
                outcome['timings'] = {
                    'judge_started_at': judge_started_at,
                    'container_acquired_at': container_acquired_at,
                    'compile_done_at': compile_done_at,
                }
                return outcome

            for index in range(1, total_cases + 1):
                if check_alive is not None:
                    check_alive()
                tc = feed.case(index)
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
                if on_case_done is not None:
                    on_case_done(case_outcomes[-1])

            tests_done_at = _utcnow_iso()
    finally:
        feed.close()
        shutil.rmtree(work_dir, ignore_errors=True)

    verdict = 'Accepted'
    for co in case_outcomes:
        if co['status'] != 'Accepted':
            verdict = co['status']
            break
    outcome = _finalize_outcome(
        case_outcomes, max_runtime, max_memory_kb, verdict_override=verdict,
    )
    outcome['timings'] = {
        'judge_started_at': judge_started_at,
        'container_acquired_at': container_acquired_at,
        'compile_done_at': compile_done_at,
        'tests_done_at': tests_done_at,
    }
    return outcome


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
        if submission.problem_type == 'interactive':
            manager_cmd, user_cmd, err = runner.compile_cpp_interactive(
                code, submission.function_files,
            )
            if err:
                return compile_failed(err)
            cfg = submission.interactive_config or {}
            try:
                num_processes = int(cfg.get('num_processes') or 1)
            except (TypeError, ValueError):
                num_processes = 1
            user_io = cfg.get('user_io') or 'fifo_io'
            return lambda stdin: runner.run_interactive(
                manager_cmd, user_cmd, stdin, num_processes, user_io,
            )
        exe, err = runner.compile_cpp(
            code,
            problem_type=submission.problem_type,
            function_files=submission.function_files,
        )
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
