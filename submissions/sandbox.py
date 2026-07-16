"""Run compile/execute steps inside an isolated Docker container with no network.

Performance / stability changes vs the previous naive approach:

* A single long-running judge container is kept alive per submission and
  reused across test cases via `docker exec`. This amortises Docker startup
  overhead (≈0.5–1.5 s) across all test cases.
* `docker info` is cached in-process (with a short TTL) instead of being
  invoked on every test case.
* The subprocess timeout honours the caller-supplied value. A small fixed
  safety margin (1 s) is added so the in-container `/usr/bin/time` report
  (the authoritative verdict) has time to be written.
* `stdin` bytes are never re-encoded; text mode is used only for stdin=None.
* Container cleanup runs once on `__exit__`; periodic housekeeping is
  handled by `submissions.docker_cleanup.cleanup_stale_judge_containers`.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

from django.conf import settings

from .docker_cleanup import cleanup_stale_judge_containers


class DockerNotAvailableError(Exception):
    pass


# ── `docker info` cache ──────────────────────────────────────────────────

_DOCKER_AVAILABLE_CACHE = {"ok": None, "ts": 0}
_DOCKER_AVAILABLE_TTL_SEC = 30


def docker_available(force_check=False):
    if not getattr(settings, "OJ_DOCKER_ENABLED", True):
        return False
    if shutil.which("docker") is None:
        return False
    entry = _DOCKER_AVAILABLE_CACHE
    now = time.monotonic()
    if (
        not force_check
        and entry["ok"] is not None
        and (now - entry["ts"]) < _DOCKER_AVAILABLE_TTL_SEC
    ):
        return entry["ok"]
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        ok = result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        ok = False
    entry["ok"] = ok
    entry["ts"] = now
    return ok


def ensure_docker_ready():
    if not docker_available():
        raise DockerNotAvailableError(
            "Docker is unavailable. Install and start Docker, then build the "
            "judge images with ./scripts/build-containers.sh."
        )


def ensure_judge_image_available(image):
    """Require a locally built image instead of allowing an implicit pull.

    A judge worker must never block while Docker tries to pull an untrusted or
    unavailable image during a submission. Images are built by deployment and
    are checked explicitly before container startup.
    """
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise DockerNotAvailableError(
            f"Could not inspect judge image {image}: {exc}"
        )
    if result.returncode != 0:
        raise DockerNotAvailableError(
            f"Judge image {image} is not installed. Run "
            "./scripts/build-containers.sh on this judge host."
        )


# ── helpers ──────────────────────────────────────────────────────────────

def _memory_flags(memory_mb):
    mem = max(int(memory_mb), 32)
    return ["--memory", f"{mem}m", "--memory-swap", f"{mem}m"]


def _runtime_user_flags():
    uid = str(getattr(settings, "OJ_DOCKER_UID", 65534))
    gid = str(getattr(settings, "OJ_DOCKER_GID", 65534))
    return ["--user", f"{uid}:{gid}"]


def _seccomp_flag(is_compile):
    base_dir = Path(__file__).resolve().parent.parent
    profile = "seccomp-compile.json" if is_compile else "seccomp-execute.json"
    return str(base_dir / "docker" / "judge" / profile)


def _prepare_work_dir(work_dir):
    try:
        os.chmod(work_dir, 0o777)
    except OSError:
        pass


def exit_indicates_memory_limit(returncode):
    return returncode in (137, -9)


def _kill_container(cid):
    if not cid:
        return
    try:
        subprocess.run(["docker", "kill", cid], capture_output=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass


# ── Long-running JudgeContainer ─────────────────────────────────────────

class JudgeContainer:
    """Keeps a single judge container alive for many ``docker exec`` calls.

    Usage::

        with JudgeContainer(work_dir, memory_mb=256, image='oj-cpp:latest', is_compile=False) as c:
            result = c.exec(['./main'], timeout_sec=2, stdin=b'1 2')

    The container is killed (at most one ``docker kill`` call) on exit.
    """

    def __init__(self, work_dir, memory_mb, image, is_compile=False):
        self.work_dir = str(Path(work_dir).resolve())
        self.memory_mb = memory_mb
        self.image = image
        self.is_compile = is_compile
        self.cid = None

    def __enter__(self):
        ensure_docker_ready()
        ensure_judge_image_available(self.image)
        _prepare_work_dir(self.work_dir)
        args = [
            "docker", "run", "--rm", "-d", "-i",
            "--network", "none",
            "--ipc", "none",
            "--hostname", "judge",
            *_memory_flags(self.memory_mb),
            *_runtime_user_flags(),
            "--pids-limit", str(getattr(settings, "OJ_DOCKER_PIDS_LIMIT", 64)),
            "--ulimit", "nofile={0}:{0}".format(
                max(16, int(getattr(settings, "OJ_DOCKER_NOFILE_LIMIT", 64)))
            ),
            "--security-opt", "no-new-privileges=true",
            "--security-opt", f"seccomp={_seccomp_flag(self.is_compile)}",
            "--cap-drop", "ALL",
            "--read-only",
            "--security-opt", "apparmor=docker-default",
            "--tmpfs", "/tmp:exec,mode=777",
            "--device", "/dev/null:r",
            "--device", "/dev/zero:r",
            "--device", "/dev/random:r",
            "--device", "/dev/urandom:r",
            "-v", f"{self.work_dir}:/sandbox:rw",
            "-w", "/sandbox",
            self.image,
            "sleep", "infinity",
        ]
        try:
            create = subprocess.run(
                args, capture_output=True, text=True, timeout=30
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise DockerNotAvailableError(
                f"Failed to start judge container: {exc}"
            )
        if create.returncode != 0:
            raise DockerNotAvailableError(
                f"Failed to start judge container: {create.stderr or create.stdout}"
            )
        self.cid = create.stdout.strip().strip('"').strip("'")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            _kill_container(self.cid)
        finally:
            self.cid = None

    def exec(self, command, timeout_sec, stdin=None):
        """Run *command* inside the running container.

        Returns a :class:`subprocess.CompletedProcess` like object.
        ``stdout`` / ``stderr`` are decoded strings when *stdin* is not bytes,
        otherwise they are bytes (matching subprocess semantics).
        """
        if not self.cid:
            raise DockerNotAvailableError("Judge container is not running")

        input_is_bytes = isinstance(stdin, (bytes, bytearray, memoryview))
        text_mode = stdin is None or not input_is_bytes
        full_cmd = ["docker", "exec", "-i", self.cid, *command]
        return subprocess.run(
            full_cmd,
            input=stdin,
            capture_output=True,
            text=text_mode,
            timeout=max(float(timeout_sec), 0.1),
        )


# ── Backward-compatible helpers ──────────────────────────────────────────

def run_in_container(
    work_dir, command, timeout_sec, stdin=None,
    memory_mb=256, image="oj-judge:latest", is_compile=False,
):
    """Run a single command in a fresh judge container."""
    with JudgeContainer(
        work_dir, memory_mb=memory_mb, image=image, is_compile=is_compile
    ) as c:
        return c.exec(command, timeout_sec, stdin=stdin)


def run_commands_in_container(
    work_dir, commands, timeout_sec, stdin=None,
    memory_mb=256, image="oj-judge:latest", is_compile=False,
):
    """Run many shell commands inside one container (single startup overhead)."""
    if not commands:
        raise ValueError('commands must not be empty')
    result = None
    with JudgeContainer(
        work_dir, memory_mb=memory_mb, image=image, is_compile=is_compile
    ) as c:
        for cmd in commands:
            result = c.exec(cmd, timeout_sec, stdin=stdin)
            if result.returncode != 0:
                return result
        assert result is not None
        return result


# ── Periodic housekeeping hook ───────────────────────────────────────────

def periodic_housekeeping():
    """Kill judge containers that have been running for too long.

    This is cheap — it is safe to call it occasionally from inside the judge
    loop. Its role is to reclaim orphans that were not cleanly shut down
    (e.g. after a worker crash). It is NOT invoked on every test case.
    """
    cleanup_stale_judge_containers()
