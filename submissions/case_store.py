"""On-disk copy of a problem's test data for the DB-less judge worker.

The judge uplink is the bottleneck of the test phase: a fat problem carries
~70 MB of test data, which is ~13 s at the measured ~2.5 MB/s, so a problem
submitted twice downloads the same bytes twice. The claim carries a content
fingerprint (see ``problems.fingerprint``) and this store files the fetched
slices under it, so a later submission reads them from local disk instead of
the network.

Layout::

    <dir>/<data_key>/<index:08d>.json    one file per test case

Files are written through a temp file and renamed into place, so a worker
killed mid-download leaves no half-written case behind. A case file is only
read for the index it was written for, and an index is stable for as long as
``data_key`` is (both derive from the same ordered stream), so a hit is always
the right data. Once the size cap is reached, the least recently used problem
directories are dropped.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

# Fingerprints arrive over the network, so the one path component we take from
# a claim must not be able to escape the cache directory.
_DATA_KEY_RE = re.compile(r'^[0-9a-f]{8,64}$')

# Reclaiming disk is a full directory walk, so it runs once per this much
# written data instead of once per fetched batch.
_PRUNE_EVERY_BYTES = 64 * 1024 * 1024

_UNITS = {'': 1, 'B': 1, 'K': 1024, 'M': 1024 ** 2, 'G': 1024 ** 3, 'T': 1024 ** 4}


def parse_size(value):
    """``'20G'`` -> bytes. ``0``/garbage means "no cap"."""
    text = str(value or '').strip().upper().rstrip('B')
    if not text:
        return 0
    unit = _UNITS.get(text[-1:], None)
    if unit is None:
        unit = 1
    else:
        text = text[:-1].strip()
    try:
        return max(int(float(text) * unit), 0)
    except (TypeError, ValueError):
        return 0


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


class CaseStore:
    """Read/write access to the local copy of one problem's test data.

    A store built for an empty fingerprint, a cache directory that is not
    configured, or a malformed fingerprint is *disabled*: every read misses
    and every write is dropped, which degrades to the uncached behaviour.
    """

    def __init__(self, data_key, directory=None, max_bytes=None):
        key = (data_key or '').strip().lower()
        root = (
            settings.OJ_CASE_CACHE_DIR
            if directory is None
            else directory
        ) or ''
        self.data_key = key
        self._root = Path(root) if root.strip() and _DATA_KEY_RE.match(key) else None
        self._max_bytes = parse_size(
            settings.OJ_CASE_CACHE_MAX_SIZE if max_bytes is None else max_bytes
        )
        self._pruned_bytes = 0

    @property
    def enabled(self):
        return self._root is not None

    def _directory(self):
        return self._root / self.data_key

    def _case_path(self, index):
        return self._directory() / f'{index:08d}.json'

    def get(self, offset, limit):
        """Return the cached slice, or ``None`` if any case is missing."""
        if not self.enabled or limit <= 0:
            return None
        rows = []
        for index in range(offset + 1, offset + limit + 1):
            try:
                with self._case_path(index).open('rb') as handle:
                    rows.append(json.loads(handle.read()))
            except (OSError, ValueError):
                return None
        self._touch()
        return rows

    def put(self, rows):
        """Store a freshly fetched slice. Best effort by design."""
        if not self.enabled or not rows:
            return
        directory = self._directory()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning('Could not create case cache %s', directory, exc_info=True)
            return
        written = 0
        for row in rows:
            try:
                index = int(row['index'])
            except (KeyError, TypeError, ValueError):
                continue
            payload = json.dumps(row, ensure_ascii=False).encode('utf-8')
            try:
                handle, tmp_name = tempfile.mkstemp(dir=directory, prefix='.tmp')
                try:
                    with os.fdopen(handle, 'wb') as stream:
                        stream.write(payload)
                    os.replace(tmp_name, self._case_path(index))
                except OSError:
                    os.unlink(tmp_name)
                    raise
            except OSError:
                logger.warning(
                    'Could not cache test case %s of problem key %s',
                    index, self.data_key, exc_info=True,
                )
                continue
            written += len(payload)
        self._touch()
        self._pruned_bytes += written
        if self._max_bytes and self._pruned_bytes >= _PRUNE_EVERY_BYTES:
            self._pruned_bytes = 0
            self._prune()

    def _touch(self):
        """Mark the entry as recently used (the prune order)."""
        try:
            os.utime(self._directory(), None)
        except OSError:
            pass

    def _prune(self):
        """Drop least recently used problems until the cap is respected."""
        try:
            entries = [
                (entry.stat().st_mtime, _dir_size(entry.path), entry.path)
                for entry in os.scandir(self._root)
                if entry.is_dir()
            ]
        except OSError:
            return
        total = sum(size for _mtime, size, _path in entries)
        entries.sort()
        for _mtime, size, path in entries:
            if total <= self._max_bytes:
                break
            shutil.rmtree(path, ignore_errors=True)
            total -= size
            logger.info('Evicted test data cache entry %s', path)
