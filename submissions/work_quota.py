"""Per-work-dir disk size caps via ext4/XFS project quotas.

The compiler and the submitted program write into the per-submission work
directory, which is bind-mounted at ``/sandbox``.  The 64 MB tmpfs cap on
``/tmp`` does not cover it, and disk-I/O BPS throttling only slows a flood
down — neither prevents a malicious compile from filling the judge host.

A *project quota* is a hard byte cap attached to a directory tree
(independent of uid): the kernel hands ``EDQUOT`` to every write (host-side
staging **and** in-container compiler output) once the tree exceeds the
limit.  Requirements:

* ext4 mounted with ``prjquota`` (or XFS with ``pquota``),
* the ``chattr`` and ``setquota`` userspace tools.

Like the other optional sandbox caps (ccache, block-I/O BPS), this module
fails open: when the feature is disabled, unsupported, or a helper errors,
judging continues and the condition is logged.  Operators see the support
state in the judge startup log / ``check_judge_health`` output.
"""

import fcntl
import logging
import os
import shutil
import subprocess

from django.conf import settings

logger = logging.getLogger(__name__)

# Project IDs below this are reserved for hand-managed assignments; the
# auto-allocator starts here to avoid clashing with them.
_PROJECT_ID_MIN = 10_000
_PROJECT_ID_MAX = 2**32 - 1

# Cache of support checks keyed by mountpoint: True / False.
_support_cache = {}
_warned = {"missing_tools": False, "limit_zero": False}


def _size_limit_mb():
    try:
        return max(int(getattr(settings, "OJ_WORKDIR_SIZE_LIMIT_MB", 1024)), 0)
    except (TypeError, ValueError):
        return 1024


def _resolve_mount(path):
    """Return ``(mountpoint, fstype, options)`` backing *path*.

    Reads /proc/mounts and picks the longest mountpoint prefix (same
    approach as ``findmnt --target``).  Returns ``None`` when unresolved.
    """
    try:
        real = os.path.realpath(path)
        best = None
        with open("/proc/mounts", "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 4:
                    continue
                _device, mountpoint, fstype, opts = parts[:4]
                # /proc/mounts octal-escapes e.g. spaces as \040.
                mountpoint = mountpoint.replace("\\040", " ")
                if real == mountpoint or real.startswith(mountpoint.rstrip("/") + "/"):
                    # ``/proc/mounts`` always carries a pseudo ``rootfs /``
                    # entry before the real root filesystem; at equal depth
                    # the later entry is the one actually visible there.
                    if best is None or len(mountpoint) >= len(best[0]):
                        best = (mountpoint, fstype, opts.split(","))
        return best
    except OSError:
        return None


def _project_quota_enabled(fstype, opts):
    if fstype == "ext4" or fstype == "ext2" or fstype == "ext3":
        return "prjquota" in opts
    if fstype == "xfs":
        return "pquota" in opts or "prjquota" in opts
    return False


def is_supported(path):
    """True when a project quota can be enforced under *path*."""
    limit_mb = _size_limit_mb()
    if limit_mb <= 0:
        if not _warned["limit_zero"]:
            logger.info("Work-dir size quota disabled (OJ_WORKDIR_SIZE_LIMIT_MB=0)")
            _warned["limit_zero"] = True
        return False
    if shutil.which("chattr") is None or shutil.which("setquota") is None:
        if not _warned["missing_tools"]:
            logger.warning(
                "Work-dir size quota unavailable: chattr/setquota not installed "
                "(apt install e2fsprogs quota)"
            )
            _warned["missing_tools"] = True
        return False
    mount = _resolve_mount(path)
    if mount is None:
        return False
    mountpoint, fstype, opts = mount
    cached = _support_cache.get(mountpoint)
    if cached is not None:
        return cached
    ok = _project_quota_enabled(fstype, opts)
    _support_cache[mountpoint] = ok
    if not ok:
        logger.warning(
            "Work-dir size quota inactive: %s (%s) is not mounted with project "
            "quota support. Remount with prjquota (ext4) or pquota (XFS), e.g. "
            "`mount -o remount,prjquota %s`.",
            mountpoint, fstype, mountpoint,
        )
    return ok


def _counter_path():
    path = str(
        getattr(
            settings,
            "OJ_QUOTA_PROJECT_ID_FILE",
            "/var/lib/guwu-oj/workdir.prjnext",
        )
    )
    return path


def _allocate_project_id():
    """Allocate a monotonically increasing project id (host-global).

    The counter file is created 0600 and updated under an flock so multiple
    worker processes never hand out the same id.
    """
    path = _counter_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 32).strip()
        try:
            current = int(raw or "")
            current = max(current, _PROJECT_ID_MIN)
        except ValueError:
            current = _PROJECT_ID_MIN
        next_id = current + 1
        if next_id > _PROJECT_ID_MAX:
            raise OSError("project id space exhausted")
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{next_id}\n".encode())
        os.fsync(fd)
        return next_id
    finally:
        os.close(fd)


def _run_helper(argv):
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=15,
    )


def apply_workdir_quota(path):
    """Cap the on-disk size of work directory *path*.

    Must be called on an empty (freshly created) directory, before any file
    is staged: the project marker is inherited by everything created
    underneath.

    Returns an opaque token ``(project_id, mountpoint)`` on success, or
    ``None`` when the cap is disabled/unsupported (judging proceeds
    uncapped).  Pass the token to :func:`remove_workdir_quota` after the
    directory is deleted.
    """
    try:
        if not is_supported(path):
            return None
        limit_mb = _size_limit_mb()
        mount = _resolve_mount(path)
        if mount is None:
            return None
        mountpoint = mount[0]

        project_id = _allocate_project_id()
        # +P marks the directory for project inheritance; -p assigns the id.
        mark = _run_helper(["chattr", "+P", "-p", str(project_id), path])
        if mark.returncode != 0:
            logger.warning(
                "chattr +P failed on %s (project quota not enforced): %s",
                path, (mark.stderr or mark.stdout).strip()[:300],
            )
            return None
        # 1 KiB blocks: bhard is the hard size cap; inode caps stay unlimited.
        blocks = limit_mb * 1024
        quota = _run_helper([
            "setquota", "-P", str(project_id),
            "0", str(blocks), "0", "0", mountpoint,
        ])
        if quota.returncode != 0:
            logger.warning(
                "setquota -P %s failed on %s (project quota not enforced): %s",
                project_id, mountpoint,
                (quota.stderr or quota.stdout).strip()[:300],
            )
            return None
        logger.debug(
            "Work-dir quota: project %s capped at %s MB on %s (%s)",
            project_id, limit_mb, mountpoint, path,
        )
        return (project_id, mountpoint)
    except Exception:
        # Never fail judging over an optional resource cap.
        logger.warning("apply_workdir_quota(%s) failed", path, exc_info=True)
        return None


def remove_workdir_quota(token):
    """Release a quota previously applied (after the directory is removed).

    The tree itself is gone by now; resetting the project entry frees the
    kernel accounting slot so the quota file cannot grow without bound.
    Best effort — stale zero-usage entries are harmless.
    """
    if not token:
        return
    project_id, mountpoint = token
    try:
        _run_helper([
            "setquota", "-P", str(project_id),
            "0", "0", "0", "0", mountpoint,
        ])
    except Exception:
        logger.debug(
            "remove_workdir_quota(%s, %s) failed",
            project_id, mountpoint, exc_info=True,
        )
