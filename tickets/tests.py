from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from devlog.models import EmailConfig
from users.sliding_window import sliding_clear

from .emails import _recipient_list, _send_ticket_email
from .forms import TicketForm
from .models import Ticket

User = get_user_model()

# 两个 ratelimit 装饰器（functools.wraps）共享同一 group；限流键落在真实
# Redis（TEST_MODE 用的就是本机 Redis），跨测试运行残留会误判 403。
_RATELIMIT_GROUP = 'tickets.views.ticket_create'


def _clear_ticket_ratelimits(user):
    sliding_clear(f'sl:rl:{_RATELIMIT_GROUP}:user:{user.pk}')
    sliding_clear(f'sl:rl:{_RATELIMIT_GROUP}:ip:127.0.0.1')


def make_user(username, email='', **extra):
    return User.objects.create_user(
        username=username, email=email, password='safe-test-password', **extra
    )


def ticket_data(**overrides):
    data = {'subject': '站点问题', 'category': 'bug', 'body': '详情内容', 'website': ''}
    data.update(overrides)
    return data


class TicketModelTests(TestCase):
    def test_defaults_and_str(self):
        user = make_user('alice')
        ticket = Ticket.objects.create(subject='题目描述有误', body='详情', submitter=user)
        self.assertEqual(ticket.status, Ticket.Status.PENDING)
        self.assertEqual(str(ticket), f'#{ticket.pk} 题目描述有误')

    def test_ordering_newest_first(self):
        user = make_user('alice')
        first = Ticket.objects.create(subject='first', body='x', submitter=user)
        second = Ticket.objects.create(subject='second', body='x', submitter=user)
        self.assertEqual(list(Ticket.objects.all()), [second, first])


class TicketFormTests(TestCase):
    def test_honeypot_filled_is_invalid(self):
        form = TicketForm(data=ticket_data(website='http://spam.example'))
        self.assertFalse(form.is_valid())
        self.assertIn('website', form.errors)

    def test_valid_without_honeypot(self):
        form = TicketForm(data=ticket_data())
        self.assertTrue(form.is_valid())


class TicketViewTests(TestCase):
    def setUp(self):
        self.user = make_user('bob', email='bob@example.com')
        self.submit_url = reverse('tickets:ticket_create')
        _clear_ticket_ratelimits(self.user)

    def tearDown(self):
        _clear_ticket_ratelimits(self.user)

    def test_anonymous_redirects_to_login(self):
        resp = self.client.get(self.submit_url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('login'), resp.url)

    def test_post_creates_ticket_and_redirects(self):
        self.client.force_login(self.user)
        with patch('tickets.views.notify_staff_ticket') as notify:
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(self.submit_url, ticket_data(contact_email=''))
        self.assertRedirects(resp, reverse('tickets:my_tickets'))
        ticket = Ticket.objects.get()
        self.assertEqual(ticket.submitter, self.user)
        self.assertEqual(ticket.contact_email, 'bob@example.com')
        self.assertEqual(ticket.status, Ticket.Status.PENDING)
        notify.assert_called_once()
        self.assertEqual(notify.call_args[0][0], ticket.pk)
        self.assertIn(f'/admin/tickets/ticket/{ticket.pk}/change/', notify.call_args[0][1])

    def test_honeypot_post_rejected(self):
        self.client.force_login(self.user)
        resp = self.client.post(self.submit_url, ticket_data(website='http://spam.example'))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Ticket.objects.exists())

    def test_rate_limit_blocks_after_5_per_hour(self):
        self.client.force_login(self.user)
        for _ in range(5):
            resp = self.client.post(self.submit_url, ticket_data())
            self.assertEqual(resp.status_code, 302)
        resp = self.client.post(self.submit_url, ticket_data())
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(Ticket.objects.count(), 5)

    def test_my_tickets_only_shows_own(self):
        other = make_user('carol', email='carol@example.com')
        Ticket.objects.create(subject='mine', body='x', submitter=self.user)
        Ticket.objects.create(subject='others', body='x', submitter=other)
        self.client.force_login(self.user)
        resp = self.client.get(reverse('tickets:my_tickets'))
        self.assertEqual(resp.status_code, 200)
        subjects = {t.subject for t in resp.context['page_obj']}
        self.assertEqual(subjects, {'mine'})


class TicketEmailTests(TestCase):
    def setUp(self):
        # close_old_connections() 会按 CONN_MAX_AGE=0 立即关闭 TestCase 的共享
        # 连接，测试中一律 patch 掉（生产环境线程内仍需要它）。
        patcher = patch('tickets.emails.close_old_connections')
        patcher.start()
        self.addCleanup(patcher.stop)
        EmailConfig.objects.create(
            pk=1,
            email_backend='django.core.mail.backends.locmem.EmailBackend',
            admin_recipients='cfg-admin@example.com\nops@example.com',
        )
        self.staff = make_user('admin1', email='Staff@Example.com', is_staff=True)
        self.normal = make_user('alice', email='alice@example.com')
        self.ticket = Ticket.objects.create(
            subject='测试工单', category='bug', body='工单正文',
            submitter=self.normal, contact_email='alice@example.com',
        )
        self.admin_url = f'http://testserver/admin/tickets/ticket/{self.ticket.pk}/change/'

    @override_settings(ADMINS=(('Root', 'Root@Example.com'),))
    def test_recipients_union_dedup(self):
        recipients = _recipient_list()
        self.assertEqual(
            recipients,
            ['cfg-admin@example.com', 'ops@example.com',
             'root@example.com', 'staff@example.com'],
        )

    @override_settings(ADMINS=(('Root', 'root@example.com'),))
    def test_send_marks_email_sent(self):
        _send_ticket_email(self.ticket.pk, self.admin_url)
        self.ticket.refresh_from_db()
        self.assertTrue(self.ticket.email_sent)
        self.assertEqual(self.ticket.email_error, '')
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertIn(f'工单 #{self.ticket.pk}', msg.subject)
        self.assertIn('问题反馈', msg.subject)
        self.assertEqual(
            sorted(msg.to),
            ['cfg-admin@example.com', 'ops@example.com',
             'root@example.com', 'staff@example.com'],
        )
        self.assertIn('工单正文', msg.body)
        self.assertIn(self.admin_url, msg.body)

    def test_send_failure_records_error(self):
        with patch('tickets.emails.EmailMultiAlternatives') as mock_cls:
            mock_cls.return_value.send.side_effect = RuntimeError('smtp down')
            _send_ticket_email(self.ticket.pk, self.admin_url)
        self.ticket.refresh_from_db()
        self.assertFalse(self.ticket.email_sent)
        self.assertIn('smtp down', self.ticket.email_error)

    def test_no_recipients_records_error(self):
        EmailConfig.objects.update(admin_recipients='')
        with patch('tickets.emails._recipient_list', return_value=[]):
            _send_ticket_email(self.ticket.pk, self.admin_url)
        self.ticket.refresh_from_db()
        self.assertFalse(self.ticket.email_sent)
        self.assertIn('未配置任何管理员收件人', self.ticket.email_error)

    def test_missing_ticket_is_tolerated(self):
        _send_ticket_email(99999, self.admin_url)  # 不应抛异常
