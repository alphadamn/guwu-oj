"""Tests for :mod:`submissions.work_quota` (per-work-dir project quotas).

The real ``chattr``/``setquota`` helpers and the kernel mount table are
mocked; these tests exercise the support detection, the host-global
project-id allocator, the helper argv construction and the fail-open
contract.
"""

import os
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from submissions import work_quota

FAKE_MOUNTS = "\n".join(
    [
        "rootfs / rootfs rw 0 0",
        "/dev/sda1 / ext4 rw,relatime,prjquota 0 0",
        "/dev/sdb1 /data xfs rw,relatime,pquota 0 0",
        "/dev/sdc1 /srv ext4 rw,relatime 0 0",
        "tmpfs /tmp tmpfs rw,nosuid,nodev 0 0",
    ]
) + "\n"


def _mounts_open(real_open, content=FAKE_MOUNTS):
    """Serve fake /proc/mounts content while leaving other opens real."""

    def _open(path, *args, **kwargs):
        if path == "/proc/mounts":
            from unittest.mock import mock_open

            return mock_open(read_data=content)()
        return real_open(path, *args, **kwargs)

    return _open


class MountDetectionTests(TestCase):
    def setUp(self):
        work_quota._support_cache.clear()

    def test_project_quota_enabled_logic(self):
        enabled = work_quota._project_quota_enabled
        self.assertTrue(enabled("ext4", ["rw", "prjquota"]))
        self.assertFalse(enabled("ext4", ["rw", "relatime"]))
        self.assertTrue(enabled("xfs", ["rw", "pquota"]))
        self.assertTrue(enabled("xfs", ["rw", "prjquota"]))
        self.assertFalse(enabled("xfs", ["rw"]))
        self.assertFalse(enabled("tmpfs", ["rw", "prjquota"]))

    def test_resolve_mount_picks_longest_prefix(self):
        import builtins

        with patch.object(builtins, "open", _mounts_open(builtins.open)):
            mountpoint, fstype, _ = work_quota._resolve_mount("/data/work/x")
        self.assertEqual(mountpoint, "/data")
        self.assertEqual(fstype, "xfs")

    def test_resolve_mount_prefers_real_rootfs_over_rootfs_pseudo_entry(self):
        # /proc/mounts always lists ``rootfs /`` before the real ext4 root.
        import builtins

        with patch.object(builtins, "open", _mounts_open(builtins.open)):
            mountpoint, fstype, _ = work_quota._resolve_mount("/root/w")
        self.assertEqual(mountpoint, "/")
        self.assertEqual(fstype, "ext4")

    def test_resolve_mount_returns_none_when_unmatched(self):
        import builtins

        with patch.object(builtins, "open", _mounts_open(builtins.open, "proc /proc proc rw 0 0\n")):
            self.assertIsNone(work_quota._resolve_mount("/data/work/x"))


class IsSupportedTests(TestCase):
    def setUp(self):
        work_quota._support_cache.clear()

    @override_settings(OJ_WORKDIR_SIZE_LIMIT_MB=0)
    def test_disabled_when_limit_zero(self):
        self.assertFalse(work_quota.is_supported("/data/w"))

    @override_settings(OJ_WORKDIR_SIZE_LIMIT_MB=1024)
    def test_missing_tools(self):
        with patch("submissions.work_quota.shutil.which", return_value=None):
            self.assertFalse(work_quota.is_supported("/data/w"))

    @override_settings(OJ_WORKDIR_SIZE_LIMIT_MB=1024)
    def test_ext4_with_and_without_prjquota(self):
        import builtins

        with patch("submissions.work_quota.shutil.which", return_value="/x"):
            with patch.object(builtins, "open", _mounts_open(builtins.open)):
                self.assertTrue(work_quota.is_supported("/root/w"))
                # Cache is keyed per mountpoint; /srv ext4 lacks prjquota.
                self.assertFalse(work_quota.is_supported("/srv/w"))

    @override_settings(OJ_WORKDIR_SIZE_LIMIT_MB=1024)
    def test_tmpfs_never_supported(self):
        import builtins

        with patch("submissions.work_quota.shutil.which", return_value="/x"):
            with patch.object(builtins, "open", _mounts_open(builtins.open)):
                self.assertFalse(work_quota.is_supported("/tmp/w"))


class ProjectIdAllocatorTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.counter = os.path.join(self.tmp, "prjnext")
        self.override = override_settings(
            OJ_QUOTA_PROJECT_ID_FILE=self.counter,
        )
        self.override.enable()
        self.addCleanup(self.override.disable)

    def test_starts_at_minimum_and_increments(self):
        self.assertEqual(work_quota._allocate_project_id(), 10_001)
        self.assertEqual(work_quota._allocate_project_id(), 10_002)
        with open(self.counter, encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "10002")

    def test_garbage_or_low_values_clamp_to_minimum(self):
        with open(self.counter, "w", encoding="utf-8") as fh:
            fh.write("not-a-number\n")
        self.assertEqual(work_quota._allocate_project_id(), 10_001)
        with open(self.counter, "w", encoding="utf-8") as fh:
            fh.write("42\n")
        self.assertEqual(work_quota._allocate_project_id(), 10_001)

    def test_concurrent_allocators_never_repeat(self):
        ids = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def allocate():
            barrier.wait()
            for _ in range(10):
                new_id = work_quota._allocate_project_id()
                with lock:
                    ids.append(new_id)

        threads = [threading.Thread(target=allocate) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(ids), 80)
        self.assertEqual(len(set(ids)), 80)


class ApplyRemoveTests(TestCase):
    def setUp(self):
        work_quota._support_cache.clear()
        self.tmp = tempfile.mkdtemp()
        self.work = os.path.join(self.tmp, "w")
        os.makedirs(self.work)
        self.calls = []

    def _fake_helper(self, rc=0):
        def helper(argv):
            self.calls.append(argv)
            return SimpleNamespace(returncode=rc, stdout="", stderr="")

        return helper

    @override_settings(OJ_WORKDIR_SIZE_LIMIT_MB=0)
    def test_unsupported_returns_none_without_helpers(self):
        self.assertIsNone(work_quota.apply_workdir_quota(self.work))
        self.assertEqual(self.calls, [])

    @override_settings(OJ_WORKDIR_SIZE_LIMIT_MB=1024)
    def test_happy_path_builds_expected_argv_and_token(self):
        with patch.object(work_quota, "is_supported", return_value=True), \
             patch.object(
                 work_quota, "_resolve_mount",
                 return_value=("/data", "xfs", ["pquota"])), \
             patch.object(work_quota, "_allocate_project_id", return_value=4242), \
             patch.object(work_quota, "_run_helper", self._fake_helper()):
            token = work_quota.apply_workdir_quota(self.work)
        self.assertEqual(token, (4242, "/data"))
        self.assertEqual(self.calls[0][:3], ["chattr", "+P", "-p"])
        self.assertIn("4242", self.calls[0])
        # setquota -P <id> 0 <1024MiB in KiB> 0 0 <mount>
        self.assertEqual(
            self.calls[1],
            ["setquota", "-P", "4242", "0", str(1024 * 1024), "0", "0", "/data"],
        )

    @override_settings(OJ_WORKDIR_SIZE_LIMIT_MB=512)
    def test_chattr_failure_fails_open(self):
        with patch.object(work_quota, "is_supported", return_value=True), \
             patch.object(
                 work_quota, "_resolve_mount",
                 return_value=("/", "ext4", ["prjquota"])), \
             patch.object(work_quota, "_allocate_project_id", return_value=7), \
             patch.object(work_quota, "_run_helper", self._fake_helper(rc=1)):
            self.assertIsNone(work_quota.apply_workdir_quota(self.work))

    @override_settings(OJ_WORKDIR_SIZE_LIMIT_MB=512)
    def test_setquota_failure_fails_open(self):
        results = [
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=1, stdout="", stderr="boom"),
        ]
        with patch.object(work_quota, "is_supported", return_value=True), \
             patch.object(
                 work_quota, "_resolve_mount",
                 return_value=("/", "ext4", ["prjquota"])), \
             patch.object(work_quota, "_allocate_project_id", return_value=7), \
             patch.object(work_quota, "_run_helper", side_effect=results):
            self.assertIsNone(work_quota.apply_workdir_quota(self.work))

    def test_unexpected_exception_fails_open(self):
        with patch.object(
            work_quota, "is_supported", side_effect=RuntimeError("nope"),
        ):
            self.assertIsNone(work_quota.apply_workdir_quota(self.work))

    def test_remove_with_empty_token_is_noop(self):
        with patch.object(work_quota, "_run_helper", self._fake_helper()):
            work_quota.remove_workdir_quota(None)
            work_quota.remove_workdir_quota(())
        self.assertEqual(self.calls, [])

    def test_remove_zeroes_the_project_entry(self):
        with patch.object(work_quota, "_run_helper", self._fake_helper()):
            work_quota.remove_workdir_quota((4242, "/data"))
        self.assertEqual(
            self.calls[0],
            ["setquota", "-P", "4242", "0", "0", "0", "0", "/data"],
        )

    def test_remove_swallows_helper_errors(self):
        def boom(argv):
            raise OSError("setquota gone")

        with patch.object(work_quota, "_run_helper", boom):
            work_quota.remove_workdir_quota((4242, "/data"))
