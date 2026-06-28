"""File-change monitoring for the guwu-oj project tree.

Maintains a baseline of file hashes (``FileSnapshot``) and, on each scan,
records any added / modified / deleted files as ``FileChange`` rows so they
can be reviewed and annotated from the developer-log page.
"""
import hashlib
import os

from django.conf import settings
from django.utils import timezone

from .models import FileChange, FileSnapshot

# Directories that are never interesting to track (dependencies, caches, vcs…).
EXCLUDED_DIRS = {
    '.git', 'venv', '.venv', 'env', '__pycache__', 'node_modules',
    'staticfiles', '.pytest_cache', '.mypy_cache', '.idea', '.vscode',
    'migrations',
}

# File suffixes / names we skip to avoid noise.
EXCLUDED_SUFFIXES = ('.pyc', '.pyo', '.log', '.sqlite3', '.swp', '.tmp')

# Skip hashing very large files; they are still tracked by size + mtime.
MAX_HASH_BYTES = 5 * 1024 * 1024


def _project_root():
    return str(getattr(settings, 'BASE_DIR'))


def _hash_file(abs_path):
    h = hashlib.sha256()
    try:
        size = os.path.getsize(abs_path)
    except OSError:
        return '', 0, 0.0
    try:
        mtime = os.path.getmtime(abs_path)
    except OSError:
        mtime = 0.0
    if size > MAX_HASH_BYTES:
        # Hash a cheap fingerprint instead of the whole file.
        h.update(f'{abs_path}:{size}:{mtime}'.encode('utf-8'))
        return h.hexdigest(), size, mtime
    try:
        with open(abs_path, 'rb') as fh:
            for chunk in iter(lambda: fh.read(65536), b''):
                h.update(chunk)
    except OSError:
        return '', size, mtime
    return h.hexdigest(), size, mtime


def _iter_project_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for name in filenames:
            if name.endswith(EXCLUDED_SUFFIXES):
                continue
            abs_path = os.path.join(dirpath, name)
            rel_path = os.path.relpath(abs_path, root)
            yield rel_path, abs_path


def scan(record_baseline_changes=False):
    """Scan the project tree and persist detected changes.

    Returns a dict summarising the scan. On the very first run (empty
    snapshot table) no ``FileChange`` rows are created unless
    ``record_baseline_changes`` is True — the baseline is just recorded.
    """
    root = _project_root()
    existing = {s.path: s for s in FileSnapshot.objects.all()}
    is_first_run = not existing

    seen = set()
    changes = []
    now = timezone.now()

    to_create_snap = []
    to_update_snap = []

    for rel_path, abs_path in _iter_project_files(root):
        seen.add(rel_path)
        file_hash, size, mtime = _hash_file(abs_path)
        snap = existing.get(rel_path)
        if snap is None:
            to_create_snap.append(FileSnapshot(
                path=rel_path, file_hash=file_hash, size=size, mtime=mtime,
            ))
            if not is_first_run or record_baseline_changes:
                changes.append(FileChange(
                    path=rel_path, change_type=FileChange.CHANGE_ADDED,
                    file_hash=file_hash, size=size, detected_at=now,
                ))
        elif snap.file_hash != file_hash:
            changes.append(FileChange(
                path=rel_path, change_type=FileChange.CHANGE_MODIFIED,
                file_hash=file_hash, old_hash=snap.file_hash, size=size, detected_at=now,
            ))
            snap.file_hash = file_hash
            snap.size = size
            snap.mtime = mtime
            to_update_snap.append(snap)

    # Detect deletions.
    deleted_paths = set(existing.keys()) - seen
    for rel_path in deleted_paths:
        snap = existing[rel_path]
        changes.append(FileChange(
            path=rel_path, change_type=FileChange.CHANGE_DELETED,
            old_hash=snap.file_hash, size=snap.size, detected_at=now,
        ))

    # Persist.
    if to_create_snap:
        FileSnapshot.objects.bulk_create(to_create_snap, batch_size=500)
    if to_update_snap:
        FileSnapshot.objects.bulk_update(
            to_update_snap, ['file_hash', 'size', 'mtime'], batch_size=500,
        )
    if deleted_paths:
        FileSnapshot.objects.filter(path__in=deleted_paths).delete()
    if changes:
        FileChange.objects.bulk_create(changes, batch_size=500)

    return {
        'first_run': is_first_run,
        'added': sum(1 for c in changes if c.change_type == FileChange.CHANGE_ADDED),
        'modified': sum(1 for c in changes if c.change_type == FileChange.CHANGE_MODIFIED),
        'deleted': sum(1 for c in changes if c.change_type == FileChange.CHANGE_DELETED),
        'total_tracked': len(seen),
        'changes_recorded': len(changes),
    }
