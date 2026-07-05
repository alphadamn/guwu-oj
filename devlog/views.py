import threading

from django.core.mail import mail_managers
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from problems.markdown_utils import render_markdown

from . import github, scanner
from .models import DevLogEntry, FileChange, ServiceComponent


def _send_health_alert(component_name, headline, detail_html=None, detail_plain=None):
    """Send a styled HTML alert email to managers **from a background thread**.

    We must never call a blocking SMTP ``connect()`` from inside the HTTP
    request/response cycle. gunicorn's SIGABRT watchdog kills the worker when
    that socket call blocks past the worker timeout, surfacing to the user as
    a 500. So the mail is dispatched from a short-lived daemon thread with a
    bounded per-attempt timeout, and any and all exceptions (including
    ``SystemExit`` / ``KeyboardInterrupt``) are swallowed.
    """
    now = timezone.localtime(timezone.now()).strftime('%Y-%m-%d %H:%M:%S')
    year = now[:4]

    header_bg = (
        'background: linear-gradient(135deg, #dc2626 0%, #ef4444 50%, #f59e0b 100%);'
        'background-color: #dc2626;'
    )
    html = f"""<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>谷物OJ 系统自检警告</title>
</head>
<body style="margin:0;padding:0;background:#f5f7fb;font-family:-apple-system,Segoe UI,PingFang SC,Hiragino Sans GB,Microsoft YaHei,Helvetica,Arial,sans-serif;color:#2d3748;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f5f7fb;">
  <tr>
    <td align="center" style="padding:24px 12px;">
      <table width="560" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;width:100%;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 8px 24px rgba(220,38,38,0.12);border:1px solid #fecaca;">
        <!-- Header -->
        <tr>
          <td align="center" style="{header_bg}padding:28px 24px;">
            <span style="display:inline-block;background:rgba(255,255,255,0.18);border:1px solid rgba(255,255,255,0.4);color:#ffffff;font-weight:600;letter-spacing:1px;padding:6px 14px;border-radius:999px;font-size:12px;text-transform:uppercase;">System Alert · 系统自检</span>
            <h1 style="margin:14px 0 6px 0;font-size:22px;font-weight:700;color:#ffffff;">⚠️ 谷物OJ 自检警告</h1>
            <p style="margin:0;color:rgba(255,255,255,0.92);font-size:14px;">{component_name} 组件未能通过健康检查</p>
          </td>
        </tr>
        <!-- Body -->
        <tr>
          <td style="padding:24px;font-size:15px;color:#2d3748;line-height:1.6;">
            <p style="margin:0 0 16px 0;"><strong>{headline}</strong></p>
            <table width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td style="padding:10px 0;border-bottom:1px dashed #e5e7eb;">
                  <span style="display:block;color:#9ca3af;font-size:12px;letter-spacing:1px;margin-bottom:4px;">检查时间</span>
                  <span style="font-size:16px;color:#111827;font-weight:600;">{now}</span>
                </td>
              </tr>
              <tr>
                <td style="padding:10px 0;">
                  <span style="display:block;color:#9ca3af;font-size:12px;letter-spacing:1px;margin-bottom:4px;">检查组件</span>
                  <span style="font-size:16px;color:#111827;font-weight:600;">{component_name}</span>
                </td>
              </tr>
            </table>
            <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:18px;">
              <tr>
                <td style="background:#fef2f2;border-left:4px solid #dc2626;border-radius:8px;padding:14px 16px;color:#7f1d1d;font-size:13px;line-height:1.6;">
                  <strong>说明：</strong>自动健康检查刚刚报告该组件不可用。请尽快登录管理后台查看详细日志并排查问题。
                  {detail_html or ''}
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <!-- Footer -->
        <tr>
          <td align="center" style="padding:16px 24px;font-size:12px;color:#9ca3af;border-top:1px solid #f3f4f6;">
            本邮件由 谷物OJ 自动发送 · 请勿直接回复<br>
            &copy; {year} 谷物OJ
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>
"""
    plain_lines = [
        f"谷物OJ 自检警告 - {component_name}",
        "",
        headline,
        f"检查时间：{now}",
        f"检查组件：{component_name}",
        "",
        "自动健康检查刚刚报告该组件不可用，请登录管理后台查看详细日志并排查问题。",
    ]
    if detail_plain:
        plain_lines.extend(["", "——", detail_plain])
    plain = "\n".join(plain_lines)
    subject = f"[谷物OJ] 自检警告：{component_name}"

    # --- Background send with hard timeout --------------------------------
    # Even when we eventually reach mail.send(), gunicorn's SIGABRT handler
    # can still turn a hanging SMTP connect into SystemExit inside this call.
    # Catch BaseException to make _send_health_alert *always* return.
    def _do_send():
        try:
            mail_managers(subject, plain, html_message=html, fail_silently=True)
        except BaseException:
            # smtplib.SMTPException, OSError, socket.timeout, SystemExit...
            pass

    thread = threading.Thread(target=_do_send, name="devlog-health-alert", daemon=True)
    thread.start()
    # Join with a short timeout so the caller never blocks past this point.
    # The daemon thread keeps running in the background if the SMTP server
    # is slow to accept the connection; if the worker restarts it is dropped.
    thread.join(timeout=1.0)


def _check_judging_system(max_wait_seconds=15):
    """Check judging system health by submitting a test solution to a hidden
    a+b problem. Returns the number of passing test cases (0 on failure).

    The test submission is deleted afterwards so it does not accumulate in
    the database across successive probes.
    """
    import time as _time
    submission = None
    try:
        from problems.models import Problem
        from submissions.models import Submission
        from submissions.judge_queue import enqueue_judge
        from django.contrib.auth import get_user_model

        User = get_user_model()

        health_problem = Problem.objects.filter(
            title='Health Check A+B', is_public=False
        ).first()
        if not health_problem:
            return 0

        health_user, _ = User.objects.get_or_create(
            username='health_check_bot',
            defaults={'is_staff': False, 'is_active': False},
        )

        correct_code = '''#include <stdio.h>
int main() {
    int a, b;
    scanf("%d %d", &a, &b);
    printf("%d\\n", a + b);
    return 0;
}'''

        submission = Submission.objects.create(
            problem=health_problem,
            user=health_user,
            code=correct_code,
            language='C',
            status='Pending',
        )

        enqueue_judge(submission.id)

        deadline = _time.monotonic() + max_wait_seconds
        while _time.monotonic() < deadline:
            submission.refresh_from_db()
            if submission.status != 'Pending':
                break
            _time.sleep(1)

        ac_count = submission.test_results.filter(status='Accepted').count()
        return int(ac_count)

    except BaseException:
        # Judge unreachable, DB gone, RQ down, a signal arriving mid-loop...
        return 0
    finally:
        # Clean up the test submission so we do not leak rows. CASCADE takes
        # care of the associated SubmissionTestResult rows.
        if submission is not None and getattr(submission, 'pk', None):
            try:
                submission.delete()
            except BaseException:
                pass


def _refresh_auto_components(force_refresh=False):
    """Update statuses of components flagged for automatic health checking.

    The actual probing is done from a short-lived daemon thread so that the
    admin action (or status page) never blocks on slow DB / Redis / judge
    / SMTP calls. gunicorn delivers SIGABRT when a request exceeds the
    worker timeout; Python's signal handler translates that into
    ``SystemExit``, which bypasses ``except Exception`` and used to surface
    to the user as a 500. Running checks in a daemon thread means the
    HTTP request always returns quickly, and the background work survives
    signal-driven exits.
    """
    # Quick out: if nothing is flagged for auto-check, don't even spawn a
    # thread.
    try:
        if ServiceComponent.objects.filter(auto_check=True).count() == 0:
            return
    except BaseException:
        return

    def _run():
        try:
            _do_refresh_auto_components(force_refresh=force_refresh)
        except BaseException:
            # DB gone, judge gone, signal delivered in the worker thread,
            # mail gone, ... — swallow it. The status page simply shows stale
            # data until the next run.
            pass

    t = threading.Thread(
        target=_run,
        name="devlog-refresh-auto-components",
        daemon=True,
    )
    t.start()
    # Bounded wait so an unlucky caller on a *really* fast DB never
    # races the "statuses not yet saved" window.
    t.join(timeout=0.5)


def _do_refresh_auto_components(force_refresh=False):
    """The actual health probes. Called only from a background thread."""
    from django.core.cache import cache

    auto = list(ServiceComponent.objects.filter(auto_check=True))
    if not auto:
        return

    cache_key = 'devlog_health_checks'
    checks = None
    if not force_refresh:
        try:
            checks = cache.get(cache_key)
        except BaseException:
            checks = None

    if checks is None:
        checks = {}
        # ----- database ---------------------------------------------------
        try:
            from django.db import connection
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1')
            checks['database'] = True
        except BaseException as e:
            checks['database'] = False
            _send_health_alert(
                component_name='数据库',
                headline='数据库无法响应健康检查 SQL（SELECT 1）',
                detail_html=f'<br><strong>异常：</strong><code style="color:#991b1b;">{e}</code>',
                detail_plain=f'异常：{e}',
            )
        # ----- redis / cache ----------------------------------------------
        try:
            cache.set('devlog_health_probe', '1')
            checks['cache'] = cache.get('devlog_health_probe') == '1'
            checks['redis'] = checks['cache']
            cache.delete('devlog_health_probe')
        except BaseException as e:
            checks['cache'] = False
            checks['redis'] = False
            _send_health_alert(
                component_name='Redis / 缓存',
                headline='缓存 (Redis) 写入或读取失败',
                detail_html=f'<br><strong>异常：</strong><code style="color:#991b1b;">{e}</code>',
                detail_plain=f'异常：{e}',
            )
        # ----- judging system ---------------------------------------------
        judge_ac_count = _check_judging_system(max_wait_seconds=15)
        checks['judge'] = judge_ac_count
        if judge_ac_count <= 1:
            _send_health_alert(
                component_name='测评系统',
                headline='健康检查用例在测评系统中通过率过低',
                detail_html=f'<br><strong>通过数量：</strong><code style="color:#991b1b;">{judge_ac_count}</code> （预期 ≥ 3）',
                detail_plain=f'通过数量：{judge_ac_count}（预期 ≥ 3）',
            )

        try:
            cache.set(cache_key, checks, timeout=1800)
        except BaseException:
            pass

    for comp in auto:
        key = comp.health_key or (comp.name_en or '').lower() or (comp.name or '').lower()
        ok = checks.get(key)

        if key == 'judge':
            if ok == 3:
                new_status = ServiceComponent.STATUS_OPERATIONAL
            elif ok == 2:
                new_status = ServiceComponent.STATUS_DEGRADED
            elif ok == 1:
                new_status = ServiceComponent.STATUS_PARTIAL
            else:
                new_status = ServiceComponent.STATUS_MAJOR
        elif ok is not None:
            new_status = (
                ServiceComponent.STATUS_OPERATIONAL
                if ok
                else ServiceComponent.STATUS_MAJOR
            )
        else:
            continue

        changed = comp.status != new_status
        if changed:
            comp.status = new_status
        if changed or force_refresh:
            try:
                comp.save(update_fields=['status', 'updated_at'])
            except BaseException:
                pass


def status_page(request):
    _refresh_auto_components()

    components = list(ServiceComponent.objects.all())
    overall = max((c.severity for c in components), default=0)
    overall_status = {
        0: ('all_operational', '所有系统正常', 'success'),
        1: ('maintenance', '维护进行中', 'info'),
        2: ('degraded', '部分系统性能下降', 'warning'),
        3: ('partial', '部分系统中断', 'warning'),
        4: ('major', '重大服务中断', 'danger'),
    }[overall]

    entries = []
    for entry in DevLogEntry.objects.select_related('author')[:30]:
        entries.append({'obj': entry, 'html': render_markdown(entry.body)})

    commits = github.get_commits(limit=15)

    file_changes = FileChange.objects.select_related('annotated_by')[:40]

    context = {
        'components': components,
        'overall_label': overall_status[1],
        'overall_class': overall_status[2],
        'overall_key': overall_status[0],
        'entries': entries,
        'commits': commits,
        'commits_page_url': github.COMMITS_PAGE_URL,
        'file_changes': file_changes,
        'last_scan': FileChange.objects.order_by('-detected_at').values_list('detected_at', flat=True).first(),
        'now': timezone.now(),
        'can_manage': request.user.is_authenticated and request.user.is_staff,
    }
    return render(request, 'devlog/status.html', context)


def _is_staff(user):
    return user.is_authenticated and user.is_staff


@require_POST
@user_passes_test(_is_staff)
def add_entry(request):
    title = (request.POST.get('title') or '').strip()
    if not title:
        messages.error(request, '标题不能为空')
        return redirect('devlog:status')
    DevLogEntry.objects.create(
        title=title,
        version=(request.POST.get('version') or '').strip(),
        body=(request.POST.get('body') or '').strip(),
        pinned=bool(request.POST.get('pinned')),
        author=request.user,
    )
    messages.success(request, '已添加开发者日志')
    return redirect('devlog:status')


@require_POST
@user_passes_test(_is_staff)
def rescan(request):
    result = scanner.scan()
    if result['first_run']:
        messages.success(
            request,
            f"已建立基线快照，共 {result['total_tracked']} 个文件（首次扫描不记录变更）。",
        )
    else:
        messages.success(
            request,
            f"扫描完成：新增 {result['added']} · 修改 {result['modified']} · 删除 {result['deleted']}。",
        )
    return redirect('devlog:status')


@require_POST
@login_required
def annotate_change(request, pk):
    if not request.user.is_staff:
        messages.error(request, '没有权限')
        return redirect('devlog:status')
    change = get_object_or_404(FileChange, pk=pk)
    change.remarks = (request.POST.get('remarks') or '').strip()[:255]
    change.description = (request.POST.get('description') or '').strip()
    change.annotated_by = request.user
    change.save(update_fields=['remarks', 'description', 'annotated_by'])
    messages.success(request, f'已更新备注：{change.path}')
    return redirect('devlog:status')


@require_POST
@user_passes_test(_is_staff)
def refresh_health_checks(request):
    """Force refresh all health checks, bypassing cache."""
    _refresh_auto_components(force_refresh=True)
    messages.success(request, '健康检查已刷新')
    return redirect('devlog:status')
