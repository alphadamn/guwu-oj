"""Admin shortcuts for backing up and restoring the database.

The 站点配置 changelist exposes two superuser-only actions. Over HTTPS a backup
is streamed to the browser; over plain HTTP it is written into the configured
server directory. Restore accepts either a file from that directory or, over
HTTPS only, an upload.

The real restore path replaces the whole database, so every test here patches
``devlog.dbbackup.restore_backup``/``create_backup`` instead of running
``psql`` against the test database.
"""

import re
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from devlog import dbbackup
from devlog.models import SiteConfig

BACKUP_URL = '/admin/devlog/siteconfig/database-backup/'
RESTORE_URL = '/admin/devlog/siteconfig/database-restore/'


class _MessageAssertions:
    """SimpleUI renders admin messages as a JSON-escaped JS array, so the text
    is not literally present in the HTML. Assert against the message store."""

    def assertMessageContains(self, response, fragment):
        messages = [str(item.message) for item in get_messages(response.wsgi_request)]
        self.assertTrue(
            any(fragment in message for message in messages),
            f'{fragment!r} not found in messages: {messages}',
        )


class _AdminBackupTestBase(_MessageAssertions, TestCase):
    def setUp(self):
        self.backup_dir = Path(tempfile.mkdtemp(prefix='oj-test-backups-'))
        self.addCleanup(shutil.rmtree, self.backup_dir, ignore_errors=True)
        SiteConfig.objects.create(pk=1, database_backup_dir=str(self.backup_dir))
        self.superuser = get_user_model().objects.create_superuser(
            username='backup-root', email='root@example.com', password='safe-test-password',
        )
        # Staff with full SiteConfig model permissions, to prove that the
        # shortcuts are gated on superuser status rather than model perms.
        self.staff = get_user_model().objects.create_user(
            username='backup-staff', password='safe-test-password', is_staff=True,
        )
        self.staff.user_permissions.add(*Permission.objects.filter(
            content_type__app_label='devlog',
            content_type__model='siteconfig',
        ))

    def login_superuser(self):
        self.client.force_login(self.superuser)

    def make_backup_file(self, name='ojdb-20260101-000000.sql', body=b'-- dump\n'):
        target = self.backup_dir / name
        target.write_bytes(body)
        return target


class BackupShortcutTests(_AdminBackupTestBase):
    def test_changelist_shows_both_shortcuts_for_superuser(self):
        self.login_superuser()
        response = self.client.get(reverse('admin:devlog_siteconfig_changelist'))
        self.assertContains(response, '备份当前数据库')
        self.assertContains(response, '从备份导入数据库')

    def test_changelist_shortcuts_are_anchors(self):
        """SimpleUI rebuilds the object-tools toolbar with

            $(".object-tools").hide().find('li a').each(...)

        so it hides the list and re-registers only ``<a href>`` descendants.
        A ``<form>``/``<button>`` shortcut renders in the HTML but never
        reaches the visible toolbar. Both shortcuts must be plain links.
        """
        self.login_superuser()
        response = self.client.get(reverse('admin:devlog_siteconfig_changelist'))
        html = response.content.decode()
        tools = re.search(
            r'<ul class="object-tools">(.*?)</ul>', html, re.DOTALL,
        )
        self.assertIsNotNone(tools, 'object-tools list not found in changelist')
        block = tools.group(1)
        for url in (BACKUP_URL, RESTORE_URL):
            self.assertRegex(
                block,
                r'<a[^>]+href="' + re.escape(url) + r'"',
                f'{url} must be reachable as an <a href> inside object-tools, '
                f'otherwise SimpleUI drops it from the toolbar.',
            )
        self.assertNotIn(
            '<form', block,
            'object-tools must not contain a form; SimpleUI would hide it.',
        )

    def test_changelist_hides_shortcuts_from_plain_staff(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse('admin:devlog_siteconfig_changelist'))
        self.assertNotContains(response, '备份当前数据库')
        self.assertNotContains(response, '从备份导入数据库')

    def test_backup_requires_superuser(self):
        self.client.force_login(self.staff)
        response = self.client.post(BACKUP_URL)
        self.assertEqual(response.status_code, 403)

    def test_restore_requires_superuser(self):
        self.client.force_login(self.staff)
        response = self.client.get(RESTORE_URL)
        self.assertEqual(response.status_code, 403)

    def test_backup_get_renders_confirmation_without_dumping(self):
        """A dump is a side effect; a bare GET must not produce one.

        GET renders the confirmation form whose own form POSTs back here. The
        page has to exist because SimpleUI rebuilds the changelist object-tools
        from ``li a`` elements only and silently drops a POST button, so the
        shortcut must be a link.
        """
        self.login_superuser()
        with mock.patch('devlog.dbbackup.create_backup') as create, \
                mock.patch('devlog.dbbackup.temporary_backup') as temporary:
            response = self.client.get(BACKUP_URL)
        self.assertEqual(response.status_code, 200)
        create.assert_not_called()
        temporary.assert_not_called()
        # The rendered page must be able to POST back to this endpoint.
        self.assertContains(response, 'csrfmiddlewaretoken')
        self.assertContains(response, '开始备份')
        self.assertContains(response, 'csrfmiddlewaretoken')

    def test_https_backup_streams_download(self):
        self.login_superuser()
        staged = Path(tempfile.mkdtemp(prefix='oj-test-staged-'))
        self.addCleanup(shutil.rmtree, staged, ignore_errors=True)
        dump = staged / 'ojdb-20260728-101500.sql'
        dump.write_bytes(b'-- streamed dump\n')

        with mock.patch('devlog.dbbackup.temporary_backup', return_value=dump):
            response = self.client.post(BACKUP_URL, secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment', response['Content-Disposition'])
        self.assertIn(dump.name, response['Content-Disposition'])
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(b''.join(response.streaming_content), b'-- streamed dump\n')

    def test_http_backup_writes_to_configured_directory(self):
        self.login_superuser()

        def fake_create(destination):
            destination = Path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b'-- server dump\n')
            return destination

        with mock.patch('devlog.dbbackup.create_backup', side_effect=fake_create), \
                mock.patch('devlog.dbbackup.temporary_backup') as temporary:
            response = self.client.post(BACKUP_URL, follow=True)

        temporary.assert_not_called()
        written = list(self.backup_dir.glob('*.sql'))
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0].read_bytes(), b'-- server dump\n')
        self.assertMessageContains(response, str(self.backup_dir))

    def test_backup_failure_is_reported_not_raised(self):
        self.login_superuser()
        with mock.patch('devlog.dbbackup.create_backup',
                        side_effect=dbbackup.BackupError('pg_dump 缺失')):
            response = self.client.post(BACKUP_URL, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertMessageContains(response, 'pg_dump 缺失')


class RestoreShortcutTests(_AdminBackupTestBase):
    def test_form_offers_upload_only_over_https(self):
        self.login_superuser()

        insecure = self.client.get(RESTORE_URL)
        self.assertContains(insecure, '已禁用上传导入')
        self.assertNotContains(insecure, 'type="file"')

        secure = self.client.get(RESTORE_URL, secure=True)
        self.assertContains(secure, 'type="file"')

    def test_form_lists_backups_from_configured_directory(self):
        self.make_backup_file('ojdb-20260101-000000.sql')
        self.make_backup_file('ignored.txt', b'not a dump')
        self.login_superuser()
        response = self.client.get(RESTORE_URL)
        self.assertContains(response, 'ojdb-20260101-000000.sql')
        self.assertNotContains(response, 'ignored.txt')

    def test_restore_from_server_path(self):
        self.make_backup_file('ojdb-20260101-000000.sql')
        self.login_superuser()
        with mock.patch('devlog.dbbackup.restore_backup') as restore:
            response = self.client.post(RESTORE_URL, {
                'source': 'path',
                'filename': 'ojdb-20260101-000000.sql',
                'confirm': '1',
            }, follow=True)
        restore.assert_called_once()
        self.assertEqual(
            Path(restore.call_args.args[0]).name, 'ojdb-20260101-000000.sql'
        )
        self.assertMessageContains(response, '已从服务器备份')

    def test_restore_requires_confirmation_checkbox(self):
        self.make_backup_file('ojdb-20260101-000000.sql')
        self.login_superuser()
        with mock.patch('devlog.dbbackup.restore_backup') as restore:
            response = self.client.post(RESTORE_URL, {
                'source': 'path',
                'filename': 'ojdb-20260101-000000.sql',
            })
        restore.assert_not_called()
        self.assertMessageContains(response, '请先勾选确认框')

    def test_restore_rejects_path_traversal(self):
        """``filename`` is client-controlled and must stay inside the directory."""
        outside = Path(tempfile.mkdtemp(prefix='oj-test-outside-'))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (outside / 'evil.sql').write_bytes(b'-- outside\n')
        self.login_superuser()
        with mock.patch('devlog.dbbackup.restore_backup') as restore:
            response = self.client.post(RESTORE_URL, {
                'source': 'path',
                'filename': f'../{outside.name}/evil.sql',
                'confirm': '1',
            })
        restore.assert_not_called()
        self.assertMessageContains(response, '已拒绝')

    def test_upload_restore_blocked_over_http(self):
        self.login_superuser()
        upload = SimpleUploadedFile('dump.sql', b'-- uploaded\n')
        with mock.patch('devlog.dbbackup.restore_backup') as restore:
            response = self.client.post(RESTORE_URL, {
                'source': 'upload', 'confirm': '1', 'file': upload,
            })
        restore.assert_not_called()
        self.assertMessageContains(response, '已禁用上传文件导入')

    def test_upload_restore_allowed_over_https(self):
        self.login_superuser()
        upload = SimpleUploadedFile('dump.sql', b'-- uploaded\n')
        with mock.patch('devlog.dbbackup.restore_backup') as restore:
            response = self.client.post(RESTORE_URL, {
                'source': 'upload', 'confirm': '1', 'file': upload,
            }, secure=True, follow=True)
        restore.assert_called_once()
        self.assertMessageContains(response, '已从上传文件')

    def test_upload_staging_is_cleaned_up(self):
        self.login_superuser()
        upload = SimpleUploadedFile('dump.sql', b'-- uploaded\n')
        seen = {}

        def record(path):
            seen['path'] = Path(path)

        with mock.patch('devlog.dbbackup.restore_backup', side_effect=record):
            self.client.post(RESTORE_URL, {
                'source': 'upload', 'confirm': '1', 'file': upload,
            }, secure=True)

        self.assertFalse(seen['path'].exists())
        self.assertFalse(seen['path'].parent.exists())


class BackupDirectoryResolutionTests(_MessageAssertions, TestCase):
    def test_default_directory_when_unset(self):
        config = SiteConfig.objects.create(pk=1, database_backup_dir='')
        self.assertEqual(config.backup_directory().name, 'database')
        self.assertEqual(config.backup_directory().parent.name, 'backups')

    def test_configured_directory_is_used(self):
        config = SiteConfig.objects.create(pk=1, database_backup_dir='/srv/oj-backups')
        self.assertEqual(config.backup_directory(), Path('/srv/oj-backups'))

    @override_settings()
    def test_relative_directory_is_rejected_by_view(self):
        SiteConfig.objects.create(pk=1, database_backup_dir='relative/path')
        superuser = get_user_model().objects.create_superuser(
            username='rel-root', email='rel@example.com', password='safe-test-password',
        )
        self.client.force_login(superuser)
        response = self.client.post(BACKUP_URL, follow=True)
        self.assertIn(
            '必须是绝对路径',
            ' '.join(str(m) for m in response.context['messages']),
        )


class UploadValidationTests(TestCase):
    def test_unexpected_extension_rejected(self):
        upload = SimpleUploadedFile('dump.zip', b'PK\x03\x04')
        with self.assertRaises(dbbackup.BackupError):
            dbbackup.stage_upload(upload)

    def test_oversized_upload_rejected(self):
        upload = SimpleUploadedFile('dump.sql', b'x' * 32)
        with mock.patch.object(dbbackup, 'MAX_UPLOAD_BYTES', 8):
            with self.assertRaises(dbbackup.BackupError):
                dbbackup.stage_upload(upload)

    def test_empty_upload_rejected(self):
        upload = SimpleUploadedFile('dump.sql', b'')
        with self.assertRaises(dbbackup.BackupError):
            dbbackup.stage_upload(upload)

    def test_valid_upload_is_staged_then_removable(self):
        upload = SimpleUploadedFile('dump.sql', b'-- ok\n')
        staged = dbbackup.stage_upload(upload)
        self.addCleanup(shutil.rmtree, staged.parent, ignore_errors=True)
        self.assertTrue(staged.is_file())
        self.assertEqual(staged.read_bytes(), b'-- ok\n')

    def test_restore_rejects_missing_and_empty_files(self):
        empty_dir = Path(tempfile.mkdtemp(prefix='oj-test-empty-'))
        self.addCleanup(shutil.rmtree, empty_dir, ignore_errors=True)
        empty = empty_dir / 'empty.sql'
        empty.write_bytes(b'')
        with self.assertRaises(dbbackup.BackupError):
            dbbackup.restore_backup(empty)
        with self.assertRaises(dbbackup.BackupError):
            dbbackup.restore_backup(empty_dir / 'nope.sql')

    def test_pg_password_never_reaches_command_line(self):
        """Credentials must travel via PGPASSWORD, not argv."""
        if dbbackup._vendor() != 'postgresql':
            self.skipTest('PostgreSQL-only behaviour')
        env, args = dbbackup._pg_env_and_args()
        password = env.get('PGPASSWORD')
        self.assertNotIn('--password', args)
        self.assertIn('--no-password', args)
        if password:
            self.assertNotIn(password, ' '.join(args))
