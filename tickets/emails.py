"""工单邮件通知。

工单保存后（``transaction.on_commit``）由 :func:`notify_staff_ticket` 起一个
短生命周期 daemon 线程发送通知邮件。Celery worker 是 DBless 的（读不到主库），
所以邮件必须在 web 进程内发送。

收件人为三类来源的去重并集：
1. ``EmailConfig.admin_recipients``（管理员后台配置的收件人列表）
2. Django ``settings.ADMINS``
3. 所有 ``is_staff=True`` 且填了邮箱的用户
"""
import logging
import threading

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.db import close_old_connections
from django.utils.html import escape
from django.utils.timezone import localtime

from devlog.email_config_helpers import (
    admin_recipient_list,
    effective_from_email,
    get_connection,
    site_name_for_email,
)

from .models import Ticket

logger = logging.getLogger(__name__)


def _recipient_list():
    """所有管理员收件人（大小写不敏感去重）。"""
    candidates = list(admin_recipient_list())
    candidates.extend(email for _, email in settings.ADMINS)
    candidates.extend(
        get_user_model().objects.filter(is_staff=True)
        .exclude(email='')
        .values_list('email', flat=True)
    )
    seen = set()
    recipients = []
    for email in candidates:
        email = (email or '').strip().lower()
        if '@' in email and email not in seen:
            seen.add(email)
            recipients.append(email)
    return recipients


def _plain_body(ticket, admin_url, site):
    email_line = ticket.contact_email or '未填写'
    return (
        f'新工单通知 — {site}\n\n'
        f'编号：#{ticket.pk}\n'
        f'分类：{ticket.get_category_display()}\n'
        f'主题：{ticket.subject}\n'
        f'提交人：{ticket.submitter.username} (ID {ticket.submitter_id})\n'
        f'联系邮箱：{email_line}\n'
        f'时间：{localtime(ticket.created_at).strftime("%Y-%m-%d %H:%M:%S")}\n\n'
        f'内容：\n{ticket.body}\n\n'
        f'处理入口：{admin_url}\n'
    )


def _html_body(ticket, admin_url, site):
    email_line = escape(ticket.contact_email or '未填写')
    rows = [
        ('工单编号', f'#{ticket.pk}'),
        ('分类', escape(ticket.get_category_display())),
        ('主题', escape(ticket.subject)),
        ('提交人', escape(f'{ticket.submitter.username} (ID {ticket.submitter_id})')),
        ('联系邮箱', email_line),
        ('提交时间', localtime(ticket.created_at).strftime('%Y-%m-%d %H:%M:%S')),
    ]
    trs = ''.join(
        f'<tr>'
        f'<td style="padding:10px 14px;color:#64748b;white-space:nowrap;'
        f'border-bottom:1px solid #e2e8f0;width:96px;font-size:13px;">{key}</td>'
        f'<td style="padding:10px 14px;color:#1e293b;'
        f'border-bottom:1px solid #e2e8f0;font-size:13px;">{value}</td>'
        f'</tr>'
        for key, value in rows
    )
    return f"""<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head><meta http-equiv="Content-Type" content="text/html; charset=utf-8" />
<title>{escape(site)} · 新工单通知</title></head>
<body style="margin:0;padding:0;background:#f5f7fb;font-family:-apple-system,Segoe UI,PingFang SC,Microsoft YaHei,Helvetica,Arial,sans-serif;color:#2d3748;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f5f7fb;">
  <tr><td align="center" style="padding:24px 12px;">
    <table width="560" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;width:100%;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 8px 24px rgba(37,99,235,0.12);border:1px solid #dbeafe;">
      <tr><td align="center" style="background:linear-gradient(135deg,#2563eb,#3b82f6);padding:28px 24px;">
        <span style="display:inline-block;background:rgba(255,255,255,0.18);border:1px solid rgba(255,255,255,0.4);color:#ffffff;font-weight:600;padding:6px 14px;border-radius:999px;font-size:12px;">Ticket · 工单</span>
        <h1 style="margin:14px 0 6px 0;font-size:20px;font-weight:700;color:#ffffff;">📮 收到新工单 #{ticket.pk}</h1>
        <p style="margin:0;color:rgba(255,255,255,0.92);font-size:14px;">{escape(site)} · 请及时处理</p>
      </td></tr>
      <tr><td style="padding:8px 12px;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">{trs}</table>
      </td></tr>
      <tr><td style="padding:6px 26px 4px 26px;">
        <p style="margin:8px 0 6px 0;font-size:13px;font-weight:600;color:#64748b;">工单内容</p>
        <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:14px 16px;font-size:13px;line-height:1.7;color:#334155;white-space:pre-wrap;word-break:break-word;">{escape(ticket.body)}</div>
      </td></tr>
      <tr><td align="center" style="padding:22px 24px 28px 24px;">
        <a href="{escape(admin_url)}" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;font-weight:600;font-size:14px;padding:11px 28px;border-radius:10px;">前往 Django 后台处理</a>
        <p style="margin:14px 0 0 0;font-size:12px;color:#94a3b8;">本邮件由系统自动发送，请勿直接回复。</p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""


def notify_staff_ticket(ticket_id, admin_url):
    """事务提交后调用：起后台线程给所有管理员发工单通知邮件。"""
    thread = threading.Thread(
        target=_send_ticket_email,
        args=(ticket_id, admin_url),
        name=f'ticket-email-{ticket_id}',
        daemon=True,
    )
    thread.start()


def _send_ticket_email(ticket_id, admin_url):
    close_old_connections()
    try:
        recipients = _recipient_list()
        if not recipients:
            Ticket.objects.filter(pk=ticket_id).update(email_error='未配置任何管理员收件人')
            return
        ticket = Ticket.objects.select_related('submitter').get(pk=ticket_id)
        site = site_name_for_email()
        subject = f'【{site} 工单 #{ticket.pk}】{ticket.get_category_display()} {ticket.subject}'
        msg = EmailMultiAlternatives(
            subject=subject,
            body=_plain_body(ticket, admin_url, site),
            from_email=effective_from_email() or None,
            to=recipients,
        )
        msg.attach_alternative(_html_body(ticket, admin_url, site), 'text/html')
        msg.connection = get_connection()
        msg.send()
        Ticket.objects.filter(pk=ticket_id).update(email_sent=True, email_error='')
    except Exception as exc:
        logger.exception('Ticket #%s email notification failed', ticket_id)
        try:
            Ticket.objects.filter(pk=ticket_id).update(email_error=str(exc)[:2000])
        except Exception:
            logger.exception('Ticket #%s failed to record email error', ticket_id)
    finally:
        close_old_connections()
