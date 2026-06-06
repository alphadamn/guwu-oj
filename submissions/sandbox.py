"""Run compile/execute steps inside an isolated Docker container with no network."""

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from datetime import datetime, timezone
import json

from dateutil import parser
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


def _base_docker_args(work_dir, timeout_sec, memory_mb, image, is_compile=False):
    work_dir = str(Path(work_dir).resolve())
    base_dir = Path(__file__).resolve().parent.parent
    
    # Choose seccomp profile based on operation type
    seccomp_profile = 'seccomp-compile.json' if is_compile else 'seccomp-execute.json'
    # seccomp_profile = 'seccomp-compile.json'
    
    return [
        'docker', 'run', '--rm', '-i',
        '--network', 'none',
        *_memory_flags(memory_mb),
        *_runtime_user_flags(),
        '--pids-limit', str(getattr(settings, 'OJ_DOCKER_PIDS_LIMIT', 128)),
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
        image
    ]


def run_in_container(work_dir, command, timeout_sec, stdin=None, memory_mb=256, image='oj-judge:latest', is_compile=False):
    """
    Run command inside the judge container.
    `command` is the argv inside the container (e.g. ['g++', ...]).
    `is_compile` determines whether to use relaxed (compile) or tight (execute) seccomp profile.
    """
    ensure_docker_ready()
    # Mounted temp dirs are often 0700 on host; relax so unprivileged
    # container user can create/read artifacts under /sandbox.
    try:
        os.chmod(work_dir, 0o777)
    except OSError:
        pass
    full_cmd = _base_docker_args(work_dir, timeout_sec, memory_mb, image, is_compile) + command
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
            # 1. Get all container IDs based on the image
            list_res = subprocess.run(
                ['docker', 'ps', '-q'],
                capture_output=True,
                text=True,
                timeout=5,
            )

            # print(list_res)

            for cid in list_res.stdout.strip().splitlines():
                # 2. Inspect the container to get its start time
                inspect_res = subprocess.run(
                    ['docker', 'inspect', cid],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                # print(inspect_res.stdout)
                if inspect_res.returncode != 0:
                    continue  # skip if inspection fails

                data = json.loads(inspect_res.stdout)
                # print(data)
                started_at_str = data[0]['State']['StartedAt']
                # Docker timestamps look like: 2026-06-06T12:34:56.789012345Z
                # started_at = datetime.fromisoformat(started_at_str.replace('Z', '+00:00'))
                started_at = parser.isoparse(started_at_str.replace('Z', '+00:00'))
                now = datetime.now(timezone.utc)
                running_seconds = (now - started_at).total_seconds()
                print(cid, running_seconds)

                # 3. Kill only if running time >= 5 seconds
                if running_seconds >= 5:
                    subprocess.run(['docker', 'kill', cid], capture_output=True, text=True)
        except Exception:
            pass  # silent failure (keeps original behaviour)
        raise exc
    finally:
        # Ensure any lingering judge containers are cleaned up after each run
        try:
            # 1. Get all container IDs based on the image
            list_res = subprocess.run(
                ['docker', 'ps', '-q'],
                capture_output=True,
                text=True,
                timeout=5,
            )

            # print(list_res)

            for cid in list_res.stdout.strip().splitlines():
                # 2. Inspect the container to get its start time
                inspect_res = subprocess.run(
                    ['docker', 'inspect', cid],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                # print(inspect_res.stdout)
                if inspect_res.returncode != 0:
                    continue  # skip if inspection fails

                data = json.loads(inspect_res.stdout)
                # print(data)
                started_at_str = data[0]['State']['StartedAt']
                # Docker timestamps look like: 2026-06-06T12:34:56.789012345Z
                # started_at = datetime.fromisoformat(started_at_str.replace('Z', '+00:00'))
                started_at = parser.isoparse(started_at_str.replace('Z', '+00:00'))
                now = datetime.now(timezone.utc)
                running_seconds = (now - started_at).total_seconds()
                print(cid, running_seconds)

                # 3. Kill only if running time >= 5 seconds
                if running_seconds >= 5:
                    subprocess.run(['docker', 'kill', cid], capture_output=True, text=True)
        except Exception:
            pass  # silent failure (keeps original behaviour


def run_commands_in_container(work_dir, commands, timeout_sec, stdin=None, memory_mb=256, image='oj-judge:latest'):
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

    full_cmd = _base_docker_args(work_dir, timeout_sec, memory_mb, image, is_compile=False) + ['/bin/sh', '-c', script]
    try:
        result = subprocess.run(
            full_cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        return result
    except Exception as exc:
        try:
            # 1. Get all container IDs based on the image
            list_res = subprocess.run(
                ['docker', 'ps', '-q'],
                capture_output=True,
                text=True,
                timeout=5,
            )

            # print(list_res)

            for cid in list_res.stdout.strip().splitlines():
                # 2. Inspect the container to get its start time
                inspect_res = subprocess.run(
                    ['docker', 'inspect', cid],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                # print(inspect_res.stdout)
                if inspect_res.returncode != 0:
                    continue  # skip if inspection fails

                data = json.loads(inspect_res.stdout)
                # print(data)
                started_at_str = data[0]['State']['StartedAt']
                # Docker timestamps look like: 2026-06-06T12:34:56.789012345Z
                # started_at = datetime.fromisoformat(started_at_str.replace('Z', '+00:00'))
                started_at = parser.isoparse(started_at_str.replace('Z', '+00:00'))
                now = datetime.now(timezone.utc)
                running_seconds = (now - started_at).total_seconds()
                print(cid, running_seconds)

                # 3. Kill only if running time >= 5 seconds
                if running_seconds >= 5:
                    subprocess.run(['docker', 'kill', cid], capture_output=True, text=True)
        except Exception:
            pass  # silent failure (keeps original behaviour
        raise exc
    finally:
        try:
            # 1. Get all container IDs based on the image
            list_res = subprocess.run(
                ['docker', 'ps', '-q'],
                capture_output=True,
                text=True,
                timeout=5,
            )

            # print(list_res)

            for cid in list_res.stdout.strip().splitlines():
                # 2. Inspect the container to get its start time
                inspect_res = subprocess.run(
                    ['docker', 'inspect', cid],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                # print(inspect_res.stdout)
                if inspect_res.returncode != 0:
                    continue  # skip if inspection fails

                data = json.loads(inspect_res.stdout)
                # print(data)
                started_at_str = data[0]['State']['StartedAt']
                # Docker timestamps look like: 2026-06-06T12:34:56.789012345Z
                # started_at = datetime.fromisoformat(started_at_str.replace('Z', '+00:00'))
                started_at = parser.isoparse(started_at_str.replace('Z', '+00:00'))
                now = datetime.now(timezone.utc)
                running_seconds = (now - started_at).total_seconds()
                print(cid, running_seconds)

                # 3. Kill only if running time >= 5 seconds
                if running_seconds >= 5:
                    subprocess.run(['docker', 'kill', cid], capture_output=True, text=True)
        except Exception:
            pass  # silent failure (keeps original behaviour


def exit_indicates_memory_limit(returncode):
    return returncode in (137, -9)
