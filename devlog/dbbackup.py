"""Database backup / restore helpers for the 站点配置 admin shortcuts.

Two entry points are used by ``devlog.admin``:

* :func:`create_backup` writes a dump of the default database either to a
  caller-supplied path or to a temporary file (for HTTPS downloads);
* :func:`restore_backup` replaces the current database contents with a dump
  produced by :func:`create_backup`.

PostgreSQL uses ``pg_dump``/``psql``; SQLite uses the stdlib backup API. Both
credentials-bearing invocations pass the password through the environment
(``PGPASSWORD``) and never through the command line, so it does not appear in
the process table.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

# A dump of a full OJ database can take a while on a large instance; keep the
# ceiling generous but bounded so a hung child process cannot pin a worker.
SUBPROCESS_TIMEOUT_SECONDS = 60 * 30

# Upload ceiling for the "import from uploaded file" path. Enforced while
# streaming as well, because ``UploadedFile.size`` comes from the client.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024

# Upload ceiling for the "import from uploaded file" path.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024

# Extensions accepted on upload / server-path import. Keyed by database vendor
# so a SQLite deployment cannot be handed a PostgreSQL script and vice versa.
ALLOWED_SUFFIXES = {
    'postgresql': ('.sql',),
    'sqlite': ('.sqlite3', '.db'),
}


class BackupError(Exception):
    """Raised when a backup or restore operation cannot be completed."""


def _vendor():
    return connection.vendor


def allowed_suffixes():
    return ALLOWED_SUFFIXES.get(_vendor(), ('.sql',))


def backup_extension():
    return allowed_suffixes()[0]


def suggested_filename():
    stamp = timezone.localtime().strftime('%Y%m%d-%H%M%S')
    name = str(connection.settings_dict.get('NAME') or 'database')
    name = Path(name).stem or 'database'
    return f'{name}-{stamp}{backup_extension()}'


def _require_tool(tool):
    resolved = shutil.which(tool)
    if not resolved:
        raise BackupError(
            f'服务器上找不到 {tool} 命令，无法执行数据库备份/导入。'
            f'请在服务器安装 postgresql-client 后重试。'
        )
    return resolved


def _tls_hostname():
    """DNS name to present to the CLI for certificate verification, if any."""
    return str(getattr(settings, 'DB_TLS_HOSTNAME', '') or '').strip()


def _pg_env_and_args():
    """Build the environment and argv shared by ``pg_dump`` and ``psql``.

    The password travels via ``PGPASSWORD`` so it never lands in the process
    table. TLS settings mirror Django's ``OPTIONS`` so the CLI verifies the
    server exactly as the application does.

    When ``settings.DB_TLS_HOSTNAME`` is set, it becomes ``PGHOST`` (the name
    checked against the certificate) while the configured ``HOST`` becomes
    ``PGHOSTADDR`` (the address actually dialed). That keeps ``verify-full``
    working on a pre-16 libpq, which cannot match IP addresses in a
    certificate SAN.
    """
    settings_dict = connection.settings_dict
    env = os.environ.copy()
    password = settings_dict.get('PASSWORD') or ''
    if password:
        env['PGPASSWORD'] = password
    options = settings_dict.get('OPTIONS') or {}
    sslmode = options.get('sslmode')
    if sslmode:
        env['PGSSLMODE'] = str(sslmode)
    sslrootcert = options.get('sslrootcert')
    if sslrootcert:
        env['PGSSLROOTCERT'] = str(sslrootcert)

    args = []
    host = str(settings_dict.get('HOST') or '')
    tls_hostname = _tls_hostname()
    if tls_hostname and host:
        env['PGHOST'] = tls_hostname
        env['PGHOSTADDR'] = host
        # PGHOST/PGHOSTADDR are dropped by an explicit --host, so omit it.
    elif host:
        args += ['--host', host]

    port = settings_dict.get('PORT')
    user = settings_dict.get('USER')
    if port:
        args += ['--port', str(port)]
    if user:
        args += ['--username', str(user)]
    args += ['--no-password', '--dbname', str(settings_dict.get('NAME') or '')]
    return env, args


def _run(command, env, stdout=None, stdin=None):
    try:
        result = subprocess.run(
            command,
            env=env,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stdin=stdin,
            stderr=subprocess.PIPE,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise BackupError(
            f'数据库操作超过 {SUBPROCESS_TIMEOUT_SECONDS // 60} 分钟仍未完成，已中止。'
        )
    except OSError as exc:
        raise BackupError(f'无法执行数据库命令：{exc}')
    if result.returncode != 0:
        detail = (result.stderr or b'').decode('utf-8', 'replace').strip()
        detail = detail[-2000:] or f'退出码 {result.returncode}'
        raise BackupError(f'数据库命令执行失败：{detail}{_tls_hostname_hint(detail)}')
    return result


def _tls_hostname_hint(detail):
    """Explain the libpq-version-specific certificate hostname failure.

    ``psycopg2`` here bundles libpq 17, which matches IP addresses in a
    certificate's SAN; the ``pg_dump``/``psql`` binaries may be older (libpq
    gained IP-SAN matching in PostgreSQL 16). So Django can connect to
    ``127.0.0.1`` under ``sslmode=verify-full`` while the CLI rejects the same
    certificate. Setting ``DB_TLS_HOSTNAME`` to a DNS name listed in the
    certificate fixes it without relaxing verification.
    """
    if 'does not match host name' not in detail and 'server certificate for' not in detail:
        return ''
    if _tls_hostname():
        return ''
    return (
        '\n提示：备份使用的 pg_dump/psql 版本较旧（libpq 16 之前不支持匹配证书中的 IP 地址），'
        '而 Django 使用的 libpq 较新，因此同一证书在此处校验失败。'
        '请在 .env 中设置 DB_TLS_HOSTNAME 为证书 SAN 中的域名（连接地址仍为 DB_HOST），'
        '即可在保留 verify-full 的前提下完成备份。'
    )


def _ensure_parent(path: Path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f'无法创建备份目录 {path.parent}：{exc}')


def create_backup(destination: Path) -> Path:
    """Dump the default database into ``destination`` and return the path."""
    destination = Path(destination)
    _ensure_parent(destination)

    if _vendor() == 'postgresql':
        pg_dump = _require_tool('pg_dump')
        env, args = _pg_env_and_args()
        # --clean --if-exists lets the restore drop existing objects first, so a
        # dump can be replayed onto a populated database.
        command = [pg_dump, '--format', 'plain', '--clean', '--if-exists', '--no-owner',
                   '--no-privileges', *args]
        try:
            with open(destination, 'wb') as handle:
                _run(command, env, stdout=handle)
        except OSError as exc:
            raise BackupError(f'无法写入备份文件 {destination}：{exc}')
    elif _vendor() == 'sqlite':
        source_name = str(connection.settings_dict.get('NAME') or '')
        if not source_name or not os.path.isfile(source_name):
            raise BackupError('找不到 SQLite 数据库文件，无法备份。')
        source = target = None
        try:
            source = sqlite3.connect(source_name)
            target = sqlite3.connect(str(destination))
            source.backup(target)
        except (sqlite3.Error, OSError) as exc:
            raise BackupError(f'SQLite 备份失败：{exc}')
        finally:
            for handle in (target, source):
                if handle is not None:
                    handle.close()
    else:
        raise BackupError(f'暂不支持对 {_vendor()} 数据库执行备份。')

    if not destination.exists() or destination.stat().st_size == 0:
        raise BackupError('备份文件为空，操作已中止。')
    return destination


def restore_backup(source: Path):
    """Replace the current database contents with the dump at ``source``."""
    source = Path(source)
    if not source.is_file():
        raise BackupError(f'备份文件不存在：{source}')
    if source.stat().st_size == 0:
        raise BackupError('备份文件为空，拒绝导入。')

    if _vendor() == 'postgresql':
        psql = _require_tool('psql')
        env, args = _pg_env_and_args()
        # ON_ERROR_STOP makes a partially applied dump fail loudly instead of
        # leaving the database half-restored without any signal.
        command = [psql, '--quiet', '--set', 'ON_ERROR_STOP=on', '--file', str(source), *args]
        connection.close()
        _run(command, env)
    elif _vendor() == 'sqlite':
        target_name = str(connection.settings_dict.get('NAME') or '')
        if not target_name:
            raise BackupError('未配置 SQLite 数据库文件路径，无法导入。')
        probe = None
        try:
            probe = sqlite3.connect(str(source))
            probe.execute('PRAGMA schema_version').fetchone()
        except sqlite3.Error as exc:
            raise BackupError(f'该文件不是有效的 SQLite 数据库：{exc}')
        finally:
            if probe is not None:
                probe.close()
        connection.close()
        try:
            shutil.copyfile(str(source), target_name)
        except OSError as exc:
            raise BackupError(f'无法写入 SQLite 数据库文件：{exc}')
    else:
        raise BackupError(f'暂不支持对 {_vendor()} 数据库执行导入。')
    connection.close()


def list_backups(directory: Path):
    """Return existing backup files in ``directory``, newest first."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    suffixes = allowed_suffixes()
    entries = []
    try:
        for item in directory.iterdir():
            if not item.is_file() or item.suffix.lower() not in suffixes:
                continue
            stat = item.stat()
            entries.append({
                'name': item.name,
                'path': str(item),
                'size': stat.st_size,
                'modified': datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.get_current_timezone()
                ),
            })
    except OSError as exc:
        raise BackupError(f'无法读取备份目录 {directory}：{exc}')
    return sorted(entries, key=lambda item: item['modified'], reverse=True)


def resolve_inside(directory: Path, name: str) -> Path:
    """Resolve ``name`` strictly inside ``directory``.

    The admin form posts a filename chosen from :func:`list_backups`, but the
    value is client-controlled, so reject anything that escapes the configured
    directory or carries an unexpected extension.
    """
    directory = Path(directory).resolve()
    candidate = (directory / name).resolve()
    if candidate.parent != directory:
        raise BackupError('备份文件路径不在配置的备份目录内，已拒绝。')
    if candidate.suffix.lower() not in allowed_suffixes():
        raise BackupError('备份文件扩展名不被支持，已拒绝。')
    if not candidate.is_file():
        raise BackupError(f'备份文件不存在：{candidate.name}')
    return candidate


def temporary_backup() -> Path:
    """Create a dump in a private temporary directory (for HTTPS download)."""
    tmpdir = Path(tempfile.mkdtemp(prefix='oj-dbbackup-'))
    return create_backup(tmpdir / suggested_filename())


def stage_upload(upload) -> Path:
    """Write an uploaded dump to a private temporary file and return its path.

    The caller is responsible for removing the returned file's parent directory.
    """
    name = Path(upload.name or '').name
    suffix = Path(name).suffix.lower()
    if suffix not in allowed_suffixes():
        raise BackupError(
            f'上传文件扩展名 {suffix or "(无)"} 不被支持，'
            f'当前数据库只接受 {"、".join(allowed_suffixes())}。'
        )
    if upload.size and upload.size > MAX_UPLOAD_BYTES:
        raise BackupError(
            f'上传文件大小超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB 上限，已拒绝。'
        )

    tmpdir = Path(tempfile.mkdtemp(prefix='oj-dbrestore-'))
    target = tmpdir / (name or f'upload{backup_extension()}')
    written = 0
    try:
        with open(target, 'wb') as handle:
            for chunk in upload.chunks():
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise BackupError(
                        f'上传文件大小超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB 上限，已拒绝。'
                    )
                handle.write(chunk)
    except BackupError:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise BackupError(f'无法保存上传的备份文件：{exc}')
    if written == 0:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise BackupError('上传的备份文件为空，已拒绝。')
    return target


def format_size(value):
    if value is None:
        return '未知'
    size = float(value)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f'{size:.1f} {unit}'
        size /= 1024
