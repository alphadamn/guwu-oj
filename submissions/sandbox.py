"""Run compile/execute steps inside an isolated Docker container with no network."""

import os
import shutil
import subprocess
from pathlib import Path

from django.conf import settings


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
    # Run as unprivileged "nobody" by default (uid/gid 65534).
    container_uid = str(getattr(settings, 'OJ_DOCKER_UID', 65534))
    container_gid = str(getattr(settings, 'OJ_DOCKER_GID', 65534))
    return ['--user', f'{container_uid}:{container_gid}']


def _base_docker_args(work_dir, timeout_sec, memory_mb):
    work_dir = str(Path(work_dir).resolve())
    base_dir = Path(__file__).resolve().parent.parent
    
    return [
        'docker', 'run', '--rm', '-i',
        '--network', 'none',
        *_memory_flags(memory_mb),
        *_runtime_user_flags(),
        '--pids-limit', str(getattr(settings, 'OJ_DOCKER_PIDS_LIMIT', 64)),
        '--security-opt', 'no-new-privileges',
        '--security-opt', f'seccomp={base_dir}/docker/judge/seccomp-profile.json',
        '--security-opt', 'apparmor=oj-judge',
        '--cap-drop', 'ALL',
        '--read-only',
        '--tmpfs', '/tmp',
        '--device', '/dev/null:r',
        '--device', '/dev/zero:r',
        '--device', '/dev/random:r',
        '--device', '/dev/urandom:r',
        '-v', f'{work_dir}:/sandbox:rw',
        '-w', '/sandbox',
        getattr(settings, 'OJ_DOCKER_IMAGE', 'oj-judge:latest'),
    ]


def run_in_container(work_dir, command, timeout_sec, stdin=None, memory_mb=256):
    """
    Run command inside the judge container.
    `command` is the argv inside the container (e.g. ['g++', ...]).
    """
    ensure_docker_ready()
    # Mounted temp dirs are often 0700 on host; relax so unprivileged
    # container user can create/read artifacts under /sandbox.
    try:
        os.chmod(work_dir, 0o777)
    except OSError:
        pass
    full_cmd = _base_docker_args(work_dir, timeout_sec, memory_mb) + command
    try:
        result = subprocess.run(
            full_cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        return result
    except subprocess.TimeoutExpired as exc:
        # On timeout, attempt to kill any lingering judge containers older than 5 seconds
        try:
            list_res = subprocess.run(
                ['docker', 'ps', '-q', '--filter', f'ancestor={getattr(settings, "OJ_DOCKER_IMAGE", "oj-judge:latest")}'],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for cid in list_res.stdout.strip().splitlines():
                subprocess.run(['docker', 'kill', cid], capture_output=True, text=True)
        except Exception:
            pass
        raise exc
    finally:
        # Ensure any lingering judge containers are cleaned up after each run
        try:
            list_res = subprocess.run(
                ['docker', 'ps', '-q', '--filter', f'ancestor={getattr(settings, "OJ_DOCKER_IMAGE", "oj-judge:latest")}'],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for cid in list_res.stdout.strip().splitlines():
                subprocess.run(['docker', 'kill', cid], capture_output=True, text=True)
        except Exception:
            pass


def run_commands_in_container(work_dir, commands, timeout_sec, stdin=None, memory_mb=256):
    """
    Run multiple commands in a single container session.
    `commands` is a list of argv lists (e.g. [['g++', ...], ['chmod', ...]]).
    This keeps the container running between commands so artifacts persist.
    """
    ensure_docker_ready()
    try:
        os.chmod(work_dir, 0o777)
    except OSError:
        pass

    # Build a shell script that runs all commands
    script_lines = ['set -e']  # Exit on error
    for cmd in commands:
        script_lines.append(' '.join(shlex.quote(arg) for arg in cmd))
    script = '\n'.join(script_lines)
    print(f"Shell script: {script}")

    full_cmd = _base_docker_args(work_dir, timeout_sec, memory_mb, is_compile=False) + ['/bin/sh', '-c', script]
    try:
        result = subprocess.run(
            full_cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        print(f"Shell script return code: {result.returncode}")
        print(f"Shell script stderr: {result.stderr}")
        print(f"Shell script stdout: {result.stdout}")
        return result
    except subprocess.TimeoutExpired as exc:
        # On timeout, attempt to kill any lingering judge containers older than 5 seconds
        try:
            # List running containers based on the judge image
            list_res = subprocess.run(
                ['docker', 'ps', '-q', '--filter', f'ancestor={getattr(settings, "OJ_DOCKER_IMAGE", "oj-judge:latest")}'],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for cid in list_res.stdout.strip().splitlines():
                # Inspect start time and kill if runtime >5s
                inspect = subprocess.run(
                    ['docker', 'inspect', '--format', '{{.State.StartedAt}}', cid],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if inspect.returncode == 0:
                    # Simple approach: always kill the container on timeout
                    subprocess.run(['docker', 'kill', cid], capture_output=True, text=True)
        except Exception:
            pass
        raise exc


def exit_indicates_memory_limit(returncode):
    return returncode in (137, -9)
