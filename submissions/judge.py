"""Judge pipeline for compiled and interpreted submissions.

Key performance / stability changes:

* **Compile once per submission** (C / C++ / Rust / Golang / Assembly /
  TypeScript / Kotlin). Previously, the combined helpers recompiled the
  whole source for every single test case, which made per-case overhead
  grow linearly with test-case count.
* **Container reuse.** On judge workers the container is handed out from a
  warm per-image container pool (``submissions.container_pool``) — no
  ``docker run`` startup overhead (≈0.5–1.5 s) per submission. Every
  checkout executes in its own ``/sandbox/<token>`` subdirectory and the
  container is sanitised (stray processes killed, tmpfs wiped) on return.
  If the pool is unavailable, a dedicated long-running container is
  started per submission (fallback) and shared across all test cases via
  ``docker exec``.
* **Honest per-case timeout.** ``_run()`` uses ``timeout_sec + 1 s`` —
  no more ``max(timeout, 5)`` inflation. The authoritative verdict
  comes from ``/usr/bin/time`` inside the container; the outer 1 s
  margin just gives the timer a chance to write its report.
* **Docker availability cached** in-process for 30 s (see
  ``submissions.sandbox.docker_available``).
* **No ``docker ps`` / ``docker inspect`` storm.** Orphan cleanup is the
  responsibility of ``submissions.docker_cleanup.cleanup_stale_judge_containers``
  (cheap periodic housekeeping), not of every single test case.
* **No spurious hello-world "warm-up".** The long-running container
  already starts once; the warm-up step was pure overhead.
* **``JudgeConfig`` read once per submission** (cached 5 minutes).
"""

import os
import os.path
import pwd
import grp
import logging
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from django.conf import settings
from django.core.cache import cache

from . import container_pool
from .claiming import ClaimLostError, finalize_claim, stamp_progress
from .models import Submission, SubmissionTestResult
from .sandbox import (
    DockerNotAvailableError,
    JudgeContainer,
    exit_indicates_memory_limit,
    run_in_container,
    run_commands_in_container,
)

logger = logging.getLogger(__name__)

JUDGED_LANGUAGES = {"C++", "Python", "Java", "C", "Assembly", "Rust",
                    "Golang", "JavaScript", "Ruby", "Kotlin"}
COMPILE_TIMEOUT_SEC = 30
HOST_TIMEOUT_SAFETY_MARGIN_SEC = 1.0
MAX_STORED_OUTPUT_LEN = 4000
JUDGE_CONFIG_CACHE_TTL = 300


LANG_IMAGE = {
    "C++": "oj-cpp:latest",
    "C": "oj-c:latest",
    "Python": "oj-python:latest",
    "Java": "oj-java:latest",
    "JavaScript": "oj-other:latest",
    "Golang": "oj-other:latest",
    "Rust": "oj-other:latest",
    "Ruby": "oj-other:latest",
    "Kotlin": "oj-other:latest",
    "Assembly": "oj-other:latest",
}


# ── small helpers ────────────────────────────────────────────────────────

def truncate_text(text, limit=MAX_STORED_OUTPUT_LEN):
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... (输出已截断)"


def normalize_output(text):
    if text is None:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip()


def outputs_match(actual, expected):
    return normalize_output(actual) == normalize_output(expected)


def extract_java_class_name(code):
    match = re.search(r"public\s+class\s+(\w+)", code)
    if match:
        return match.group(1)
    match = re.search(r"class\s+(\w+)", code)
    if match:
        return match.group(1)
    return "Main"


def _clean_kotlin_output(text):
    """Strip JVM deprecation warnings from kotlinc output."""
    if not text:
        return ""
    lines = []
    for line in text.strip().splitlines():
        if "OpenJDK" in line and "warning" in line:
            continue
        if "Picked up JAVA_TOOL_OPTIONS" in line:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _get_judge_config_global_timeout():
    """Read ``JudgeConfig.subprocess_timeout_sec`` (cached) or fall back to 5."""
    try:
        cfg = cache.get("judge_config")
        if cfg is None:
            from .models import JudgeConfig
            cfg = JudgeConfig.objects.first()
            if cfg is not None:
                cache.set("judge_config", cfg, timeout=JUDGE_CONFIG_CACHE_TTL)
        return int(getattr(cfg, "subprocess_timeout_sec", 5) or 5)
    except Exception:
        return int(getattr(settings, "OJ_SUBPROCESS_TIMEOUT_SEC", 5) or 5)


# ── SandboxRunner ────────────────────────────────────────────────────────

class SandboxRunner:
    """Runs the compilation + per-test-case steps of one submission.

    The runner uses either a warm container checked out from the per-image
    pool (``pool_handle``; zero ``docker run`` overhead) or, as a fallback,
    a dedicated long-running container started in ``__enter__``. The
    container is reused for every compile/execute command of the
    submission, so the amortised startup overhead is ≈0 for N test cases.
    """

    def __init__(self, work_dir, time_limit_ms, memory_limit_mb, image,
                 pool_handle=None, exec_workdir=None, global_timeout_sec=None):
        self.work_dir = work_dir
        self.time_limit_sec = max(float(time_limit_ms) / 1000.0, 0.1)
        self.memory_limit_mb = max(int(memory_limit_mb), 32)
        self.image = image
        self.pool_handle = pool_handle
        # Pooled containers mount one shared host root at /sandbox; each
        # submission executes in its own /sandbox/<token> subdirectory.
        self.exec_workdir = exec_workdir
        self.last_memory_kb = None
        self._container = None
        # Explicit override (DB-less workers receive it in the claim bundle);
        # lazily read from JudgeConfig/cache when None.
        self._global_timeout_sec = global_timeout_sec

    # ── context manager ──────────────────────────────────────────────────
    def chown_rec(self, path, user, group):
        uid = pwd.getpwnam(user).pw_uid
        gid = grp.getgrnam(group).gr_gid

        for root, dirs, files in os.walk(path):
            os.chown(root, uid, gid)          # 处理当前目录
            for name in dirs + files:         # 处理所有子项（目录和文件）
                os.chown(os.path.join(root, name), uid, gid)
    
    def __enter__(self):
        try:
            self.chown_rec(self.work_dir, "nobody", "nogroup")
            os.chmod(self.work_dir, 0o754)
        except OSError:
            pass
        if self.pool_handle is not None:
            # Warm container: no docker run / image check needed here.
            self._container = self.pool_handle
        else:
            self._container = JudgeContainer(
                self.work_dir,
                memory_mb=max(self.memory_limit_mb, 512),
                image=self.image,
                is_compile=True,
            ).__enter__()
        # Read JudgeConfig.global_timeout_sec (cached) once per submission
        # unless an explicit override was provided (DB-less workers).
        if self._global_timeout_sec is None:
            self._global_timeout_sec = _get_judge_config_global_timeout()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._container is None:
            return
        if self.pool_handle is not None:
            # Any exception means the container state is suspect (e.g. the
            # docker exec transport failed mid-run) — recycle rather than
            # hand a possibly-corrupt container to the next submission.
            container_pool.release(
                self.pool_handle, force_destroy=exc_type is not None
            )
            self.pool_handle = None
        else:
            self._container.__exit__(exc_type, exc_val, exc_tb)
        self._container = None

    # ── time / memory parsing ───────────────────────────────────────────

    def _parse_time_stderr(self, stderr):
        elapsed_ms = None
        memory_kb = None
        if not stderr:
            return elapsed_ms, memory_kb
        for line in stderr.splitlines():
            gnu_match = re.search(r"OJ_TIME\s+(\d+)\s+([\d.]+)", line)
            if gnu_match:
                memory_kb = int(gnu_match.group(1))
                elapsed_ms = int(float(gnu_match.group(2)) * 1000)
                continue
            bash_match = re.search(r"real\s+(\d+)m(\d+(?:\.\d+)?)s", line)
            if bash_match:
                elapsed_ms = int(
                    (int(bash_match.group(1)) * 60 + float(bash_match.group(2))) * 1000
                )
        return elapsed_ms, memory_kb

    def _timed_command(self, cmd):
        cmd_str = " ".join(shlex.quote(arg) for arg in cmd)
        return ["/bin/bash", "-lc", f"/usr/bin/time -f \"OJ_TIME %M %e\" {cmd_str}"]

    # ── low-level runner ────────────────────────────────────────────────

    def _run(self, command, timeout_sec, stdin=None, is_compile=False):
        """Execute a command inside the long-running container.

        The real timeout is *timeout_sec* (no inflation) plus a small
        safety margin so ``/usr/bin/time`` always writes its report.
        """
        if self._container is None:
            raise DockerNotAvailableError("Judge container is not running")

        # Compilation is generally heavier than the problem memory limit,
        # so we already bumped memory at container creation time. This
        # flag is kept for API compatibility.
        del is_compile

        padded = float(timeout_sec) + HOST_TIMEOUT_SAFETY_MARGIN_SEC
        return self._container.exec(
            command, padded, stdin=stdin, workdir=self.exec_workdir
        )

    # ── compile steps (run once per submission) ────────────────────────

    def compile_cpp(self, code):
        src = Path(self.work_dir) / "main.cpp"
        src.write_text(code, encoding="utf-8")
        try:
            res = self._run(
                ["g++", "-std=c++17", "-O2", "-o", "main", "main.cpp"],
                COMPILE_TIMEOUT_SEC,
                is_compile=True,
            )
        except subprocess.TimeoutExpired:
            return None, "Compile timeout"
        if res.returncode != 0:
            return None, (res.stderr or res.stdout or "Compilation failed").strip()
        # chmod via host filesystem (work_dir is bind-mounted).
        try:
            os.chmod(Path(self.work_dir) / "main", 0o700)
        except OSError:
            pass
        return "./main", None

    def compile_c(self, code):
        src = Path(self.work_dir) / "main.c"
        src.write_text(code, encoding="utf-8")
        try:
            res = self._run(
                ["gcc", "-O2", "-o", "main", "main.c"],
                COMPILE_TIMEOUT_SEC,
                is_compile=True,
            )
        except subprocess.TimeoutExpired:
            return None, "Compile timeout"
        if res.returncode != 0:
            return None, (res.stderr or res.stdout or "Compilation failed").strip()
        try:
            os.chmod(Path(self.work_dir) / "main", 0o700)
        except OSError:
            pass
        return "./main", None

    def compile_rust(self, code):
        src = Path(self.work_dir) / "main.rs"
        src.write_text(code, encoding="utf-8")
        try:
            res = self._run(
                ["rustc", "--edition=2021", "-o", "main", "main.rs"],
                COMPILE_TIMEOUT_SEC,
                is_compile=True,
            )
        except subprocess.TimeoutExpired:
            return None, "Compile timeout"
        if res.returncode != 0:
            return None, (res.stderr or res.stdout or "Compilation failed").strip()
        try:
            os.chmod(Path(self.work_dir) / "main", 0o700)
        except OSError:
            pass
        return "./main", None

    def compile_golang(self, code):
        src = Path(self.work_dir) / "main.go"
        src.write_text(code, encoding="utf-8")
        try:
            res = self._run(
                ["go", "build", "-o", "main", "main.go"],
                COMPILE_TIMEOUT_SEC,
                is_compile=True,
            )
        except subprocess.TimeoutExpired:
            return None, "Compile timeout"
        if res.returncode != 0:
            return None, (res.stderr or res.stdout or "Compilation failed").strip()
        try:
            os.chmod(Path(self.work_dir) / "main", 0o700)
        except OSError:
            pass
        return "./main", None

    def compile_assembly(self, code):
        src = Path(self.work_dir) / "main.s"
        src.write_text(code, encoding="utf-8")
        try:
            as_res = self._run(
                ["as", "-o", "main.o", "main.s"],
                COMPILE_TIMEOUT_SEC,
                is_compile=True,
            )
            if as_res.returncode != 0:
                return None, (as_res.stderr or as_res.stdout or "Assemble failed").strip()
            link_res = self._run(
                ["ld", "-o", "main", "main.o"],
                COMPILE_TIMEOUT_SEC,
                is_compile=True,
            )
            if link_res.returncode != 0:
                return None, (link_res.stderr or link_res.stdout or "Link failed").strip()
        except subprocess.TimeoutExpired:
            return None, "Compile timeout"
        try:
            os.chmod(Path(self.work_dir) / "main", 0o700)
        except OSError:
            pass
        return "./main", None

    def compile_java(self, code):
        class_name = extract_java_class_name(code)
        #src = Path(self.work_dir) / f"{class_name}.java"
        src = os.path.realpath(os.path.join(self.work_dir, f"{class_name}.java"))
        if not src.startswith(self.work_dir):
            return None, "Invalid file path"
        src = Path(src)
        src.write_text(code, encoding="utf-8")
        try:
            res = self._run(
                ["javac", f"{class_name}.java"],
                COMPILE_TIMEOUT_SEC,
                is_compile=True,
            )
        except subprocess.TimeoutExpired:
            return None, "Compile timeout"
        if res.returncode != 0:
            return None, (res.stderr or res.stdout or "Compilation failed").strip()
        return class_name, None

    def compile_typescript(self, code):
        src = Path(self.work_dir) / "main.ts"
        src.write_text(code, encoding="utf-8")
        try:
            res = self._run(
                ["tsc", "--target", "ES2022", "--module", "commonjs",
                 "--skipLibCheck", "main.ts"],
                COMPILE_TIMEOUT_SEC,
                is_compile=True,
            )
        except subprocess.TimeoutExpired:
            return None, "Compile timeout"
        if res.returncode != 0:
            return None, (res.stderr or res.stdout or "Compilation failed").strip()
        return "node ./main.js", None

    def compile_kotlin(self, code):
        src = Path(self.work_dir) / "main.kt"
        src.write_text(code, encoding="utf-8")
        try:
            res = self._run(
                ["kotlinc", "main.kt", "-include-runtime", "-d", "main.jar"],
                COMPILE_TIMEOUT_SEC,
                is_compile=True,
            )
        except subprocess.TimeoutExpired:
            return None, "Compile timeout"
        if res.returncode != 0:
            return None, _clean_kotlin_output(
                res.stderr or res.stdout or "Compilation failed"
            )
        return "java -jar main.jar", None

    # ── execute step (per test case) ────────────────────────────────────

    def run_executable(self, cmd, stdin_data):
        """Run *cmd* (shell tokens) with the problem's time limit.

        If the subprocess is killed before ``/usr/bin/time`` writes its
        report line (e.g. an infinite loop), elapsed_ms is reported as
        the configured problem time-limit so the test-case runtime is
        not ``None``.
        """
        fallback_ms = int(self.time_limit_sec * 1000)
        try:
            wrapped_cmd = self._timed_command(cmd)
            result = self._run(wrapped_cmd, self.time_limit_sec, stdin=stdin_data)
            elapsed_ms, memory_kb = self._parse_time_stderr(result.stderr)
            self.last_memory_kb = memory_kb
        except subprocess.TimeoutExpired:
            # Host killed the process because it exceeded the outer timeout.
            return None, fallback_ms, "Time Limit Exceeded"

        if elapsed_ms is None:
            # /usr/bin/time didn't leave us a report — the program was
            # killed mid-execution (e.g. the host safety margin, or the
            # kernel killed it for memory). Fall back to problem limit
            # so callers see a non-None runtime.
            elapsed_ms = fallback_ms

        if exit_indicates_memory_limit(result.returncode):
            return None, elapsed_ms, "Memory Limit Exceeded"
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "Runtime error").strip()
            return None, elapsed_ms, ("Runtime Error", err)
        return result.stdout, elapsed_ms, None


# ── verdict / storage helpers ───────────────────────────────────────────

def save_case_result(submission, tc, case_index, status, runtime,
                     actual, expected, error_message=""):
    """Persist one per-case verdict, idempotent on (submission, case_index).

    Duplicate delivery inside the same claim therefore collapses onto the
    same row instead of violating the unique constraint.
    """
    relation_field = (
        'contest_test_case' if submission.contest_problem_id is not None
        else 'test_case'
    )
    SubmissionTestResult.objects.update_or_create(
        submission=submission,
        case_index=case_index,
        defaults={
            relation_field: tc,
            'status': status,
            'runtime': runtime,
            'actual_output': truncate_text(actual),
            'expected_output': '',
            'error_message': truncate_text(error_message, 2000),
        },
    )


def commit_verdict(submission, verdict, claim, runtime=None, memory=None,
                   failed=False):
    """Write the terminal verdict through the claim fence.

    With an active claim the UPDATE is conditional on (token, JUDGING); a
    stale worker that lost its lease raises :class:`ClaimLostError` and its
    side effects never run. Without a claim (legacy direct calls, e.g.
    unit tests) the plain ORM write path is preserved.
    """
    if claim is None:
        submission.status = verdict
        if runtime is not None:
            submission.runtime = runtime
        if memory is not None:
            submission.memory = memory
        fields = ['status']
        if runtime is not None:
            fields.append('runtime')
        if memory is not None:
            fields.append('memory')
        submission.save(update_fields=fields)
        return

    won = finalize_claim(
        submission.id, claim.token, verdict,
        runtime=runtime, memory=memory, failed=failed,
    )
    if not won:
        raise ClaimLostError(
            f'fenced verdict write rejected for submission {submission.id} '
            f'(token {claim.token})'
        )
    submission.status = verdict
    submission.judge_state = 'FAILED' if failed else 'DONE'
    if runtime is not None:
        submission.runtime = runtime
    if memory is not None:
        submission.memory = memory


def finalize_submission(submission, case_results, max_runtime,
                        max_memory_kb, problem, claim=None):
    verdict = 'Accepted'
    for status in case_results:
        if status != "Accepted":
            verdict = status
            break

    if verdict != 'Accepted':
        commit_verdict(
            submission, verdict, claim,
            runtime=max_runtime or 0, memory=max_memory_kb or None,
        )
        return

    commit_verdict(
        submission, 'Accepted', claim,
        runtime=max_runtime or 0, memory=max_memory_kb or None,
    )

    # Side effects run only after the fenced write won; both are
    # intrinsically idempotent (M2M add, and apply_points keyed on
    # (user, event_type, event_key)), so a re-judged Accepted stays a
    # single credit.
    if submission.contest_problem_id is None:
        submission.user.solved_problems.add(problem)
        from points.models import PointConfig
        from points.services import apply_points

        reward_points = PointConfig.get_solo().accepted_testcase_points
        if reward_points:
            for result in submission.test_results.filter(status='Accepted').select_related('test_case'):
                if result.test_case_id and not result.test_case.is_sample:
                    apply_points(
                        user_id=submission.user_id,
                        amount=reward_points,
                        event_type='accepted_testcase',
                        event_key=f'{problem.id}:{result.test_case_id}',
                        description=f'首次通过 {problem.title} 的测试点 #{result.case_index}',
                    )


def save_compile_error(submission, test_case, error_message, claim=None):
    """Persist compiler output as the first-case diagnostic for a submission."""
    stamp_phase(submission, claim, compile_done_at=None)
    save_case_result(
        submission, test_case, 1, "Skipped", None,
        error_message, test_case.expected_output, error_message,
    )
    commit_verdict(submission, "Compile Error", claim)
    return submission


def stamp_phase(submission, claim, **fields):
    """Best-effort lifecycle timing stamp; never fails the judge run.

    A failed stamp (DB blip, lost fence) only loses analysis data, which
    must never turn a judgeable submission into a System Error.
    """
    try:
        stamp_progress(
            submission.id,
            claim.token if claim is not None else None,
            **fields,
        )
    except Exception:
        logger.warning(
            'Timing stamp %s failed for submission %s',
            sorted(fields), submission.id, exc_info=True,
        )


def _case_status_from_error(error, actual, expected):
    if error == "Time Limit Exceeded":
        return "Time Limit Exceeded"
    if error == "Memory Limit Exceeded":
        return "Memory Limit Exceeded"
    if isinstance(error, tuple) and error[0] == "Runtime Error":
        return "Runtime Error", error[1]
    if not outputs_match(actual, expected):
        return "Wrong Answer"
    return "Accepted"


# ── main entry point ─────────────────────────────────────────────────────

def judge_submission(submission_id, claim=None):
    submission = Submission.objects.select_related("problem", "contest_problem", "user").get(
        id=submission_id
    )
    problem = submission.effective_problem
    # This worker is now actually on the job; the gap from claimed_at is
    # scheduling/pickup overhead.
    stamp_phase(submission, claim, judge_started_at=None)
    # Fresh attempt: the claim winner owns the row, so removing the
    # previous attempt's partial case rows is safe.
    SubmissionTestResult.objects.filter(submission=submission).delete()

    def _permanent_system_error(reason):
        logger.error('Submission %s -> System Error: %s', submission_id, reason)
        commit_verdict(submission, "System Error", claim, runtime=0)

    if problem is None:
        _permanent_system_error('no judging target')
        return submission

    if submission.language not in JUDGED_LANGUAGES:
        _permanent_system_error(f'unsupported language {submission.language}')
        return submission

    test_cases = list(problem.test_cases.all())
    if not test_cases:
        # A terminal status avoids submissions polling forever when a problem
        # was published before test data was configured.
        _permanent_system_error('no test cases configured')
        return submission

    image = LANG_IMAGE.get(submission.language, "oj-judge:latest")

    # Prefer a warm container from the per-image pool. Pooled containers
    # bind-mount one shared host root at /sandbox; each submission gets an
    # unguessable subdirectory there. Fallback: ephemeral container with its
    # own temp dir, exactly as before.
    pool_handle = container_pool.acquire(
        image, memory_mb=max(int(problem.memory_limit), 512)
    )
    exec_workdir = None
    try:
        if pool_handle is not None:
            logger.debug("Submission %s using warm pooled container %s",
                         submission_id, pool_handle.cid[:12])
            token = secrets.token_hex(8)
            work_dir = os.path.join(pool_handle.host_root, token)
            os.makedirs(work_dir, mode=0o750, exist_ok=False)
            exec_workdir = f"/sandbox/{token}"
        else:
            work_dir = tempfile.mkdtemp(prefix="oj_judge_")
    except BaseException:
        # Never leak a checked-out pool slot if setup fails before the
        # runner context manager takes ownership.
        container_pool.release(pool_handle, force_destroy=True)
        raise
    max_runtime = 0
    max_memory_kb = 0
    case_statuses = []

    try:
        runner = SandboxRunner(
            work_dir,
            problem.time_limit,
            problem.memory_limit,
            image=image,
            pool_handle=pool_handle,
            exec_workdir=exec_workdir,
        )

        # ── Start long-running container + compile once ───────────────

        with runner:
            if submission.language == "C++":
                exe, err = runner.compile_cpp(submission.code)
                if err:
                    return save_compile_error(submission, test_cases[0], err, claim=claim)
                run_fn = lambda stdin: runner.run_executable([exe], stdin)

            elif submission.language == "C":
                exe, err = runner.compile_c(submission.code)
                if err:
                    return save_compile_error(submission, test_cases[0], err, claim=claim)
                run_fn = lambda stdin: runner.run_executable([exe], stdin)

            elif submission.language == "Rust":
                exe, err = runner.compile_rust(submission.code)
                if err:
                    return save_compile_error(submission, test_cases[0], err, claim=claim)
                run_fn = lambda stdin: runner.run_executable([exe], stdin)

            elif submission.language == "Golang":
                exe, err = runner.compile_golang(submission.code)
                if err:
                    return save_compile_error(submission, test_cases[0], err, claim=claim)
                run_fn = lambda stdin: runner.run_executable([exe], stdin)

            elif submission.language == "Assembly":
                exe, err = runner.compile_assembly(submission.code)
                if err:
                    return save_compile_error(submission, test_cases[0], err, claim=claim)
                run_fn = lambda stdin: runner.run_executable([exe], stdin)

            elif submission.language == "Java":
                class_name, err = runner.compile_java(submission.code)
                if err:
                    return save_compile_error(submission, test_cases[0], err, claim=claim)
                run_fn = lambda stdin: runner.run_executable(["java", class_name], stdin)

            elif submission.language == "Kotlin":
                _, err = runner.compile_kotlin(submission.code)
                if err:
                    return save_compile_error(submission, test_cases[0], err, claim=claim)
                run_fn = lambda stdin: runner.run_executable(
                    ["java", "-jar", "main.jar"], stdin
                )

            elif submission.language == "Python":
                filename = f"{submission.user_id}_{int(time.time() * 1000)}.py"
                src = Path(work_dir) / filename
                src.write_text(submission.code, encoding="utf-8")
                run_fn = lambda stdin: runner.run_executable(["python3", filename], stdin)

            elif submission.language == "JavaScript":
                filename = f"{submission.user_id}_{int(time.time() * 1000)}.js"
                src = Path(work_dir) / filename
                src.write_text(submission.code, encoding="utf-8")
                run_fn = lambda stdin: runner.run_executable(["node", filename], stdin)

            elif submission.language == "Ruby":
                filename = f"{submission.user_id}_{int(time.time() * 1000)}.rb"
                src = Path(work_dir) / filename
                src.write_text(submission.code, encoding="utf-8")
                run_fn = lambda stdin: runner.run_executable(["ruby", filename], stdin)

            else:
                return submission

            # Compilation (if any) is done; every branch below runs tests.
            stamp_phase(submission, claim, compile_done_at=None)

            # ── Run each test case inside the SAME container ────────
            for idx, tc in enumerate(test_cases, start=1):
                # Abort immediately if the reaper revoked our lease while a
                # previous case was running.
                if claim is not None:
                    claim.ensure_alive()
                runner.last_memory_kb = None
                stdout, elapsed_ms, error = run_fn(tc.input_data)
                actual = stdout if stdout is not None else ""
                expected = tc.expected_output

                if elapsed_ms:
                    max_runtime = max(max_runtime, elapsed_ms)
                if runner.last_memory_kb:
                    max_memory_kb = max(max_memory_kb, runner.last_memory_kb)

                # A watchdog-terminated process can report exactly the limit
                # after /usr/bin/time rounds to milliseconds. Its nonzero exit
                # must remain TLE rather than being recorded as RE.
                if (
                    elapsed_ms
                    and elapsed_ms >= problem.time_limit
                    and isinstance(error, tuple)
                    and error[0] == "Runtime Error"
                ):
                    error = "Time Limit Exceeded"

                parsed = _case_status_from_error(error, actual, expected)
                if isinstance(parsed, tuple):
                    case_status, error_msg = parsed
                    actual = actual or error_msg
                else:
                    case_status = parsed
                    error_msg = ""

                save_case_result(
                    submission, tc, idx, case_status, elapsed_ms,
                    actual, expected, error_msg,
                )
                case_statuses.append(case_status)

            # All cases executed; only container teardown and the verdict
            # writeback are left.
            stamp_phase(submission, claim, tests_done_at=None)

        finalize_submission(submission, case_statuses, max_runtime,
                            max_memory_kb, problem, claim=claim)

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    return submission
