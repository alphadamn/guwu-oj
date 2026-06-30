"""Run compile/execute steps inside an isolated Docker container with no network."""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from django.conf import settings

from submissions.docker_cleanup import cleanup_stale_judge_containers


class DockerNotAvailableError(Exception):
    pass


def docker_available():
    if not getattr(settings, 'OJ_DOCKER_ENABLED', True):
        return False
    if shutil.which('docker') is None:
        return False
    try:
        result = subprocess.run(
            ['docker', 'info'],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def ensure_docker_ready():
    if not docker_available():
        raise DockerNotAvailableError(
            'Docker 不可用。请安装并启动 Docker，然后执行: '
            'docker build -t oj-judge:latest docker/judge'
        )


def _memory_flags(memory_mb):
    mem = max(int(memory_mb), 32)
    return [
        '--memory', f'{mem}m',
        '--memory-swap', f'{mem}m',
    ]


def _runtime_user_flags():
    container_uid = str(getattr(settings, 'OJ_DOCKER_UID', 65534))
    container_gid = str(getattr(settings, 'OJ_DOCKER_GID', 65534))
    return ['--user', f'{container_uid}:{container_gid}']


def _base_docker_args(work_dir, timeout_sec, memory_mb, image, is_compile=False):
    work_dir = str(Path(work_dir).resolve())
    base_dir = Path(__file__).resolve().parent.parent
    seccomp_profile = 'seccomp-compile.json' if is_compile else 'seccomp-execute.json'

    return [
        'docker', 'run', '--rm', '-i',
        '--network', 'none',
        *_memory_flags(memory_mb),
        *_runtime_user_flags(),
        '--pids-limit', str(getattr(settings, 'OJ_DOCKER_PIDS_LIMIT', 64)),
        '--security-opt', 'no-new-privileges',
        '--security-opt', f'seccomp={base_dir}/docker/judge/{seccomp_profile}',
        '--cap-drop', 'ALL',
        '--read-only',
        '--tmpfs', '/tmp:exec,mode=777',
        '--device', '/dev/null:r',
        '--device', '/dev/zero:r',
        '--device', '/dev/random:r',
        '--device', '/dev/urandom:r',
        '-v', f'{work_dir}:/sandbox:rw',
        '-w', '/sandbox',
        image,
    ]


def _prepare_work_dir(work_dir):
    try:
        os.chmod(work_dir, 0o777)
    except OSError:
        pass


def run_in_container(work_dir, command, timeout_sec, stdin=None, memory_mb=256, image='oj-judge:latest', is_compile=False):
    """Run command inside the judge container."""
    ensure_docker_ready()
    _prepare_work_dir(work_dir)
    full_cmd = _base_docker_args(work_dir, timeout_sec, memory_mb, image, is_compile) + command
    try:
        return subprocess.run(
            full_cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        cleanup_stale_judge_containers()
        raise exc
    finally:
        cleanup_stale_judge_containers()


def run_commands_in_container(work_dir, commands, timeout_sec, stdin=None, memory_mb=256, image='oj-judge:latest'):
    """Run multiple commands in a single container session."""
    ensure_docker_ready()
    _prepare_work_dir(work_dir)

    script_lines = ['set -e']
    for cmd in commands:
        script_lines.append(' '.join(shlex.quote(arg) for arg in cmd))
    script = '\n'.join(script_lines)

    full_cmd = _base_docker_args(work_dir, timeout_sec, memory_mb, image, is_compile=False) + ['/bin/sh', '-c', script]
    try:
        return subprocess.run(
            full_cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except Exception as exc:
        cleanup_stale_judge_containers()
        raise exc
    finally:
        cleanup_stale_judge_containers()


def exit_indicates_memory_limit(returncode):
    return returncode in (137, -9)
