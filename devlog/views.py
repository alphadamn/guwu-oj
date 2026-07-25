import json
import threading
from decimal import Decimal, InvalidOperation

from django.core.mail import mail_managers
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from problems.markdown_utils import render_markdown

from . import github, scanner
from .models import (
    CacheConfig,
    DevLogEntry,
    FileChange,
    HealthCheckConfig,
    HealthSample,
    RegistrationConfig,
    ServiceComponent,
    TrafficBrowserLocationMetric,
)


@csrf_protect
@require_POST
def record_browser_location(request):
    """Store only a consented, one-decimal browser location point."""
    if request.COOKIES.get('oj_analytics_consent') != 'accepted':
        return JsonResponse({'detail': 'Analytics consent required'}, status=403)
    from .models import SiteConfig
    if not SiteConfig.browser_geolocation_is_enabled():
        return JsonResponse({'detail': 'Browser geolocation is disabled'}, status=403)
    try:
        payload = json.loads(request.body.decode('utf-8'))
        latitude = Decimal(str(payload['latitude'])).quantize(Decimal('0.1'))
        longitude = Decimal(str(payload['longitude'])).quantize(Decimal('0.1'))
    except (KeyError, TypeError, ValueError, InvalidOperation, json.JSONDecodeError):
        return JsonResponse({'detail': 'Invalid coordinates'}, status=400)
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return JsonResponse({'detail': 'Invalid coordinates'}, status=400)
    from django.db import IntegrityError
    from django.db.models import F
    today = timezone.localdate()
    updated = TrafficBrowserLocationMetric.objects.filter(day=today, latitude=latitude, longitude=longitude).update(requests=F('requests') + 1)
    if not updated:
        try:
            TrafficBrowserLocationMetric.objects.create(day=today, latitude=latitude, longitude=longitude, requests=1)
        except IntegrityError:
            TrafficBrowserLocationMetric.objects.filter(day=today, latitude=latitude, longitude=longitude).update(requests=F('requests') + 1)
    response = JsonResponse({'ok': True})
    response.set_cookie('oj_browser_location', f'{latitude},{longitude}', max_age=31536000, samesite='Lax')
    return response


# Cache keys written by the devlog module. ``clear_devlog_cache`` drops
# them all, and admin actions also call it.
DEVLOG_CACHE_KEYS = (
    'devlog_health_checks',
    'devlog_health_probe',
    'devlog_github_commits',
)


# 以下 prefix 是页面渲染（markdown、问题列表/通过率）、搜索、devlog、
# 首页 stats 等所有视图级别写的缓存 key。"清除页面缓存" 按钮会用 Redis
# KEYS + DELETE 批量删除它们。
#
# 注：`@cache_page` 装饰器在 Django 默认配置下会写 key 形如
#   `:1:views.decorators.cache.cache_page.<method_prefix>.<md5>`
# 所以我们也把它包含在内。
PAGE_CACHE_KEY_PATTERNS = (
    'markdown_render_*',
    'problem_list_*',
    'problem_pass_rate_*',
    'home_recent_problems',
    'home_stats',
    'search:results:*',
    'views.decorators.cache.cache_*',
    'devlog_github_commits',
    'devlog_health_probe',
    'devlog_health_checks',
    'judge_config',
    'judge:sub_machine:*',
    'health_check',
)
# 需要同时匹配 "views.decorators.cache.cache_*" 与带有 ":1:" 前缀的版本
# 这里同时加上带有 :1: 前缀的 pattern 以确保两种都能清理。
# 实际删除函数会对每个 pattern 同时尝试原始形式与 ":1:..." 前缀形式。


def _load_config(klass):
    """Return ``klass.objects.get_or_create(pk=1)``.

    Any exception during the DB round-trip is swallowed so the rest of
    the application still works while migrations are pending or the DB
    is momentarily unreachable. Callers should *always* use ``getattr``
    with a fallback default value when reading fields on the returned
    instance.
    """
    try:
        obj, _created = klass.objects.get_or_create(pk=1)
        return obj
    except Exception:
        return None


def load_cache_config():
    return _load_config(CacheConfig)


def load_health_config():
    return _load_config(HealthCheckConfig)


def load_registration_config():
    return _load_config(RegistrationConfig)


# ---------------------------------------------------------------------
# Public helpers — cache clearing and registration feature-gates.
# ---------------------------------------------------------------------

def _try_delete_pattern(pattern: str) -> int:
    """Best-effort delete by Redis KEYS pattern.

    - 如果 django-redis 可用，直接 ``KEYS pattern`` -> ``DELETE matched``。
    - 否则回退到 ``cache.delete_pattern``（仅本地/文件缓存后端有效）。
    - 会同时尝试原 pattern 与 ``*:1:pattern``，兼容不同版本的 ``KEY_FUNCTION``。
    """
    total = 0
    try:
        from django_redis import get_redis_connection
        try:
            con = get_redis_connection('default')
            for p in (pattern, f'*:1:{pattern.lstrip(":")}', f'*:{pattern}'):
                matched = con.keys(p)
                if matched:
                    total += int(con.delete(*matched) or 0)
            return total
        except BaseException:
            pass
    except Exception:
        pass
    try:
        if hasattr(_djcache, 'delete_pattern'):
            total += int(_djcache.delete_pattern(pattern) or 0)
    except BaseException:
        pass
    return total


def clear_devlog_cache() -> int:
    """Drop every cache key written by the devlog module. Returns the number of
    keys actually removed (best-effort)."""
    removed = 0
    try:
        for key in DEVLOG_CACHE_KEYS:
            try:
                if _djcache.delete(key):
                    removed += 1
            except BaseException:
                pass
    except BaseException:
        pass
    return removed


def clear_page_cache() -> int:
    """Drop every page-cache entry produced by ``@cache_page`` /
    ``CacheMiddleware``, regardless of view. Returns number of keys deleted."""
    total = 0
    for pattern in PAGE_CACHE_KEY_PATTERNS:
        try:
            total += int(_try_delete_pattern(pattern))
        except BaseException:
            pass
    return total


def clear_all_caches() -> int:
    """Convenience helper — clear devlog cache + page cache. Returns total."""
    try:
        return int(clear_devlog_cache()) + int(clear_page_cache())
    except BaseException:
        return 0


def registration_email_verification_required():
    """True when the admin panel has toggled "注册需邮箱验证" on."""
    cfg = load_registration_config()
    if cfg is None:
        return True  # safe default: on
    return bool(getattr(cfg, 'email_verification_required', True))


def registration_enabled():
    """True when the admin panel has toggled "允许注册" on."""
    cfg = load_registration_config()
    if cfg is None:
        return True
    return bool(getattr(cfg, 'registration_enabled', True))


# ---------------------------------------------------------------------
# Health / alerting helpers.
# ---------------------------------------------------------------------

def _send_health_alert(component_name, headline, detail_html=None, detail_plain=None, sync=False):
    """Send a styled HTML alert email.

    Recipients = Django ``settings.MANAGERS`` union ``EmailConfig.admin_recipients``
    (the latter is editable from the admin panel).

    Args:
        component_name: Human-readable component name for the alert headline.
        headline: One-line alert summary used in the body.
        detail_html: Optional rich-text details (inserted into the HTML body).
        detail_plain: Optional plain-text details (included after the separator).
        sync: If True, send synchronously (important for cron jobs / one-off
            processes where the parent process might exit before a daemon
            thread finishes). Default False (background thread).
    """
    try:
        cfg = load_health_config()
        if cfg is not None and not getattr(cfg, 'health_alert_enabled', True):
            return
    except BaseException:
        pass

    # --- Apply admin-managed SMTP settings (so admin changes take effect) ---
    extra_recipients: list[str] = []
    site_name = 'Guwu Online Judge'
    try:
        from devlog.email_config_helpers import (
            apply_email_settings, site_name_for_email, admin_recipient_list,
        )
        apply_email_settings()
        site_name = site_name_for_email()
        extra_recipients = admin_recipient_list()
    except BaseException:
        pass

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
<title>{site_name} 系统自检警告</title>
</head>
<body style="margin:0;padding:0;background:#f5f7fb;font-family:-apple-system,Segoe UI,PingFang SC,Hiragino Sans GB,Microsoft YaHei,Helvetica,Arial,sans-serif;color:#2d3748;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f5f7fb;">
  <tr>
    <td align="center" style="padding:24px 12px;">
      <table width="560" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;width:100%;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 8px 24px rgba(220,38,38,0.12);border:1px solid #fecaca;">
        <tr>
          <td align="center" style="{header_bg}padding:28px 24px;">
            <span style="display:inline-block;background:rgba(255,255,255,0.18);border:1px solid rgba(255,255,255,0.4);color:#ffffff;font-weight:600;letter-spacing:1px;padding:6px 14px;border-radius:999px;font-size:12px;text-transform:uppercase;">System Alert · 系统自检</span>
            <h1 style="margin:14px 0 6px 0;font-size:22px;font-weight:700;color:#ffffff;">⚠️ {site_name} 自检警告</h1>
            <p style="margin:0;color:rgba(255,255,255,0.92);font-size:14px;">{component_name} 组件未能通过健康检查</p>
          </td>
        </tr>
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
        <tr>
          <td align="center" style="padding:16px 24px;font-size:12px;color:#9ca3af;border-top:1px solid #f3f4f6;">
            本邮件由 {site_name} 自动发送 · 请勿直接回复<br/>
            &copy; {year} {site_name}
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
        f"{site_name} 自检警告 - {component_name}",
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
    subject = f"[{site_name}] 自检警告：{component_name}"

    # --- Compose recipient list: MANAGERS + admin_recipient_list ---
    recipients: list[str] = []
    try:
        from django.conf import settings as _dj_settings
        for entry in getattr(_dj_settings, 'MANAGERS', []) or []:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                recipients.append(str(entry[1]))
            elif isinstance(entry, str) and '@' in entry:
                recipients.append(entry)
    except BaseException:
        pass
    if extra_recipients:
        recipients.extend(extra_recipients)
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_recipients: list[str] = []
    for addr in recipients:
        if addr and addr not in seen:
            seen.add(addr)
            unique_recipients.append(addr)

    def _do_send():
        if not unique_recipients:
            return
        try:
            from django.core.mail import EmailMultiAlternatives
            from django.conf import settings as _dj_settings
            from_email = getattr(_dj_settings, 'DEFAULT_FROM_EMAIL', None) or None
            msg = EmailMultiAlternatives(
                subject=subject,
                body=plain,
                from_email=from_email,
                to=unique_recipients,
            )
            msg.attach_alternative(html, "text/html")
            msg.send(fail_silently=True)
        except BaseException:
            pass

    if sync:
        _do_send()
    else:
        thread = threading.Thread(
            target=_do_send, name="devlog-health-alert", daemon=True,
        )
        thread.start()


def _check_judging_system(max_wait_seconds=None):
    """Check judging system health with an admin-tunable wait time."""
    if max_wait_seconds is None:
        try:
            cfg = load_health_config()
            if cfg is not None:
                max_wait_seconds = int(getattr(cfg, 'judge_max_wait_seconds', 15))
        except BaseException:
            max_wait_seconds = 15
    if max_wait_seconds is None or max_wait_seconds < 1:
        max_wait_seconds = 15

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
        print(ac_count)
        return int(ac_count)

    except BaseException:
        try:
            return submission.test_results.filter(status='Accepted').count()
        except:
            return 0
    finally:
        if submission is not None and getattr(submission, 'pk', None):
            try:
                submission.delete()
            except BaseException:
                pass


def _refresh_auto_components(force_refresh=False):
    """Entry point for the health-check refresh.

    * ``force_refresh=True`` (cron jobs / admin button): run the probes
      synchronously in the caller's thread. Any exceptions raised by the
      probes are logged (but not re-raised) so the job shows as completed.
    * ``force_refresh=False`` (page view): launch a short-lived background
      thread that only re-runs the probes when the cached results are stale.
    """
    if force_refresh:
        try:
            _do_refresh_auto_components(force_refresh=True, sync_alert=True)
        except BaseException as exc:
            import logging
            logging.getLogger(__name__).error(
                "Health-check refresh failed: %s", exc, exc_info=True,
            )
        return

    try:
        if ServiceComponent.objects.filter(auto_check=True).count() == 0:
            return
    except BaseException:
        return

    def _run():
        try:
            _do_refresh_auto_components(force_refresh=False)
        except BaseException:
            pass

    t = threading.Thread(
        target=_run,
        name="devlog-refresh-auto-components",
        daemon=True,
    )
    t.start()


def _do_refresh_auto_components(force_refresh=False, sync_alert=False):
    """Probe all auto-check components.

    When ``force_refresh=False`` the cached result is returned if present
    (avoiding re-probes on every page view). ``force_refresh=True`` (used
    by the cron job and admin button) always re-probes.

    ``sync_alert=True`` tells ``_send_health_alert`` to send mail
    synchronously (important for one-off processes like cron jobs).
    """
    health_cfg = load_health_config()
    cache_cfg = load_cache_config()

    try:
        if cache_cfg is not None:
            cache_ttl = int(getattr(cache_cfg, 'health_cache_seconds', 1800)) or 1800
        else:
            cache_ttl = 1800
    except BaseException:
        cache_ttl = 1800

    try:
        if health_cfg is not None:
            sample_retention_days = int(getattr(health_cfg, 'sample_retention_days', 37))
        else:
            sample_retention_days = 37
    except BaseException:
        sample_retention_days = 37
    if sample_retention_days < 1:
        sample_retention_days = 1

    try:
        if health_cfg is not None:
            judge_pass = int(getattr(health_cfg, 'judge_pass_score', 3))
            judge_deg = int(getattr(health_cfg, 'judge_score_degraded', 2))
            judge_partial = int(getattr(health_cfg, 'judge_score_partial', 1))
        else:
            judge_pass, judge_deg, judge_partial = 3, 2, 1
    except BaseException:
        judge_pass, judge_deg, judge_partial = 3, 2, 1

    auto = list(ServiceComponent.objects.filter(auto_check=True))
    if not auto:
        return

    cache_key = 'devlog_health_checks'
    checks = None
    # Track whether we actually ran a fresh probe. Only create new
    # HealthSample records for real probes (not cache hits). This
    # prevents repeated page refreshes from polluting the uptime
    # stats with stale data.
    did_fresh_probe = False
    if not force_refresh:
        try:
            checks = _djcache.get(cache_key)
        except BaseException:
            checks = None

    if checks is None:
        did_fresh_probe = True
        checks = {}
        # database
        try:
            from django.db import connection
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1')
            checks['database'] = True
        except BaseException as e:
            checks['database'] = False
            _send_health_alert(
                component_name='数据库',
                headline='数据库无法响应健康检查 SQL (SELECT 1)',
                detail_html=f'<br/><strong>异常：</strong><code style="color:#991b1b;">{e}</code>',
                detail_plain=f'异常：{e}',
                sync=sync_alert,
            )
        # Redis: use the configured Redis client directly.  The Django cache
        # wrapper can be unavailable or stale independently of a healthy Redis.
        try:
            from django_redis import get_redis_connection
            import uuid

            redis_client = get_redis_connection('default')
            redis_client.ping()
            probe_key = f'devlog:health:{uuid.uuid4().hex}'
            redis_client.set(probe_key, '1', ex=15)
            checks['redis'] = redis_client.get(probe_key) in (b'1', '1')
            redis_client.delete(probe_key)
        except BaseException as e:
            checks['redis'] = False
            _send_health_alert(
                component_name='Redis',
                headline='Redis 直接 Ping 或读写健康检查失败',
                detail_html=f'<br/><strong>异常：</strong><code style="color:#991b1b;">{e}</code>',
                detail_plain=f'异常：{e}',
                sync=sync_alert,
            )

        # Cache remains an independent application-level capability.
        try:
            _djcache.set('devlog_health_probe', '1', timeout=15)
            checks['cache'] = _djcache.get('devlog_health_probe') == '1'
            _djcache.delete('devlog_health_probe')
        except BaseException:
            checks['cache'] = False
        # judging system
        judge_ac_count = _check_judging_system()
        checks['judge'] = judge_ac_count
        if judge_ac_count <= 1:
            _send_health_alert(
                component_name='测评系统',
                headline='健康检查用例在测评系统中通过率过低',
                detail_html=f'<br/><strong>通过数量：</strong><code style="color:#991b1b;">{judge_ac_count}</code> (预期 >= {judge_pass})',
                detail_plain=f'通过数量：{judge_ac_count} (预期 >= {judge_pass})',
                sync=sync_alert,
            )

        try:
            _djcache.set(cache_key, checks, timeout=cache_ttl)
        except BaseException:
            pass

    # Only write samples / update status when we actually ran the
    # probes. Cache hits must not alter DB state.
    if not did_fresh_probe:
        return

    now = timezone.now()
    samples_to_create = []
    comps_to_save = []

    for comp in auto:
        key = comp.health_key or (comp.name_en or '').lower() or (comp.name or '').lower()
        raw = checks.get(key)

        if key == 'judge':
            score = int(raw) if isinstance(raw, int) else (0 if raw is None else int(raw or 0))
            ok = score >= judge_pass
            if score >= judge_pass:
                new_status = ServiceComponent.STATUS_OPERATIONAL
            elif score == judge_deg:
                new_status = ServiceComponent.STATUS_DEGRADED
            elif score == judge_partial:
                new_status = ServiceComponent.STATUS_PARTIAL
            else:
                new_status = ServiceComponent.STATUS_MAJOR
        elif raw is None:
            continue
        else:
            ok = bool(raw)
            score = 1 if ok else 0
            new_status = (
                ServiceComponent.STATUS_OPERATIONAL
                if ok
                else ServiceComponent.STATUS_MAJOR
            )

        samples_to_create.append(
            HealthSample(component=comp, ok=ok, score=score, sampled_at=now)
        )

        if comp.status != new_status:
            comp.status = new_status
            comps_to_save.append(comp)
        elif force_refresh:
            comps_to_save.append(comp)

    try:
        if samples_to_create:
            HealthSample.objects.bulk_create(samples_to_create, batch_size=50)
    except BaseException:
        pass

    try:
        for comp in comps_to_save:
            comp.save(update_fields=['status', 'updated_at'])
    except BaseException:
        pass

    try:
        cutoff = now - timezone.timedelta(days=sample_retention_days)
        HealthSample.objects.filter(sampled_at__lt=cutoff).delete()
    except BaseException:
        pass


def status_page(request):
    _refresh_auto_components()
    health_cfg = load_health_config()

    try:
        if health_cfg is not None:
            window_days = int(getattr(health_cfg, 'uptime_window_days', 30)) or 30
        else:
            window_days = 30
    except BaseException:
        window_days = 30
    if window_days < 1:
        window_days = 30

    components = []
    for c in ServiceComponent.objects.all():
        try:
            computed = c.uptime_90_days(window_days=window_days)
        except Exception:
            computed = None
        if computed is None:
            try:
                pct = float(c.uptime or 100.0)
            except (TypeError, ValueError):
                pct = 100.0
        else:
            pct = computed
        # Clamp to [0, 100] so every row's uptime-bar length is visually
        # consistent regardless of which window is configured.
        pct = max(0.0, min(100.0, float(pct)))
        display_text = f'{pct:.2f}%'
        components.append({
            'obj': c,
            'name': c.name,
            'description': c.description,
            'auto_check': c.auto_check,
            'status_display': c.get_status_display(),
            'badge_class': c.badge_class,
            'uptime_pct': pct,
            'uptime_display': display_text,
        })

    overall = max((c['obj'].severity for c in components), default=0)
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

    try:
        if health_cfg is not None:
            fcd = int(getattr(health_cfg, 'file_change_days', 30)) or 30
        else:
            fcd = 30
        cutoff = timezone.now() - timezone.timedelta(days=fcd)
        file_changes_qs = FileChange.objects.filter(detected_at__gte=cutoff)
    except BaseException:
        file_changes_qs = FileChange.objects.all()
    file_changes_raw = list(file_changes_qs.select_related('annotated_by')[:40])
    file_changes = []
    for fc in file_changes_raw:
        file_changes.append({
            'obj': fc,
            'path': fc.path,
            'change_type': fc.change_type,
            'badge_class': fc.badge_class,
            'change_type_display': fc.get_change_type_display(),
            'detected_at': fc.detected_at,
            'remarks': fc.remarks,
            'annotated_by': fc.annotated_by,
            'description_html': render_markdown(fc.description or ''),
            'description_plain': fc.description or '',
            'pk': fc.pk,
        })

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
        'uptime_window_days': window_days,
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
    _refresh_auto_components(force_refresh=True)
    messages.success(request, '健康检查已刷新')
    return redirect('devlog:status')


@require_POST
@user_passes_test(_is_staff)
def clear_cache(request):
    removed = clear_devlog_cache()
    messages.success(request, f'缓存已清除，共清除 {removed} 个缓存键')
    return redirect('devlog:status')


@require_POST
@user_passes_test(_is_staff)
def clear_page_cache_view(request):
    """Drop all cache_page entries across the entire site."""
    removed = clear_page_cache()
    messages.success(request, f'页面缓存已清除，共清除 {removed} 个缓存键')
    return redirect('devlog:status')


@require_POST
@user_passes_test(_is_staff)
def clear_all_caches_view(request):
    """Drop both page-cache and devlog caches."""
    removed = clear_all_caches()
    messages.success(request, f'已清除页面缓存 + devlog 缓存，共 {removed} 个缓存键')
    return redirect('devlog:status')
