from collections import OrderedDict
from datetime import timedelta
from pathlib import Path
import json
import os
import shutil

from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.http import FileResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils import timezone

from .models import (
    CacheConfig,
    CaptchaConfig,
    DevLogEntry,
    EmailConfig,
    FileChange,
    FileSnapshot,
    HealthCheckConfig,
    HealthSample,
    RegistrationConfig,
    ServiceComponent,
    SiteConfig,
    TrafficBrowserLocationMetric,
    TrafficCountryMetric,
    TrafficDailyMetric,
    TrafficPageMetric,
)

# --- SimpleUI / Django Admin 全局标题与首页品牌 ---
admin.site.site_header = '谷物 OJ 管理中心'
admin.site.site_title = '谷物 OJ 后台'
admin.site.index_title = '欢迎使用谷物 OJ — 快速查看系统运行状态与配置'
admin.site.enable_nav_sidebar = True

# Existing admin implementation follows.


def env_generator_view(request):
    """Render the browser-only deployment environment-file generator."""
    return render(request, 'admin/devlog/env_generator.html', {
        'title': '.env 配置生成器',
    })


def _last_days(days=14):
    today = timezone.localdate()
    return [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]


def _series_for_days(rows, field, days=14):
    values = {row['day']: row[field] for row in rows}
    return [values.get(day, 0) for day in _last_days(days)]


def _database_size_bytes():
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_database_size(current_database())')
            return int(cursor.fetchone()[0] or 0)
    name = str(connection.settings_dict.get('NAME') or '')
    try:
        return os.path.getsize(name) if name and os.path.isfile(name) else None
    except OSError:
        return None


def _format_bytes(value):
    if value is None:
        return '不可用'
    units = ('B', 'KB', 'MB', 'GB', 'TB')
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f'{size:.1f} {unit}'
        size /= 1024


def dashboard_metrics_view(request):
    from problems.models import Problem
    from submissions.models import Submission
    from users.models import User

    days = _last_days()
    start = days[0]
    traffic_queryset = TrafficDailyMetric.objects.filter(day__gte=start)
    traffic_rows = list(traffic_queryset.values('day', 'page_views'))
    traffic = _series_for_days(traffic_rows, 'page_views')
    traffic_started_at = TrafficDailyMetric.objects.order_by('day').values_list('day', flat=True).first()
    submissions = _series_for_days(
        Submission.objects.filter(created_at__date__gte=start)
        .annotate(day=TruncDate('created_at'))
        .values('day').annotate(total=Count('id')),
        'total',
    )
    verdict_rows = Submission.objects.values('status').annotate(total=Count('id'))
    verdicts = OrderedDict((row['status'], row['total']) for row in verdict_rows)
    total_submissions = sum(verdicts.values())
    accepted = verdicts.get('Accepted', 0)
    components = list(ServiceComponent.objects.values('name', 'status'))
    operational = sum(
        component['status'] == ServiceComponent.STATUS_OPERATIONAL
        for component in components
    )

    page_rows = list(
        TrafficPageMetric.objects.filter(day__gte=start)
        .values('path').annotate(page_views=Sum('page_views'))
        .order_by('-page_views', 'path')[:5]
    )
    top_problems = list(
        Submission.objects.filter(problem__is_public=True)
        .values('problem_id', 'problem__title')
        .annotate(submissions=Count('id'))
        .order_by('-submissions', 'problem_id')[:5]
    )
    for problem in top_problems:
        problem['id'] = problem.pop('problem_id')
        problem['title'] = problem.pop('problem__title')

    country_rows = list(
        TrafficCountryMetric.objects.filter(day__gte=start)
        .values('country_code', 'country_name', 'latitude', 'longitude')
        .annotate(requests=Sum('requests'))
        .order_by('-requests', 'country_code')[:100]
    )

    browser_rows = list(
        TrafficBrowserLocationMetric.objects.filter(day__gte=start)
        .values('latitude', 'longitude')
        .annotate(requests=Sum('requests'))
        .order_by('-requests', 'latitude', 'longitude')[:100]
    )
    if browser_rows and SiteConfig.browser_geolocation_is_enabled():
        from devlog.geoip import country_for_coordinates

        browser_locations = []
        for row in browser_rows:
            country = country_for_coordinates(row['latitude'], row['longitude']) or {
                'country_code': 'BROWSER',
                'country_name': '浏览器授权位置',
            }
            browser_locations.append({
                **country,
                'latitude': float(row['latitude']),
                'longitude': float(row['longitude']),
                'requests': row['requests'],
            })
        locations = browser_locations
        location_source = 'browser'
    else:
        locations = country_rows
        location_source = 'ip'

    location_list = list(locations)
    if location_source == 'browser':
        grouped = {}
        for item in locations:
            key = item['country_code']
            if key not in grouped:
                grouped[key] = {
                    'country_code': key,
                    'country_name': item['country_name'],
                    'requests': 0,
                }
            grouped[key]['requests'] += item['requests']
        location_list = sorted(
            grouped.values(),
            key=lambda item: (-item['requests'], item['country_code']),
        )[:100]

    from devlog.geoip import server_location
    destination = server_location()

    return JsonResponse({
        'labels': [day.strftime('%m-%d') for day in days],
        'traffic': traffic,
        'location_has_data': bool(locations),
        'locations': locations,
        'location_list': location_list,
        'location_source': location_source,
        'location_mode': 'browser' if SiteConfig.browser_geolocation_is_enabled() else 'ip',
        'server_location': destination,
        'server_location_has_data': bool(destination),
        'traffic_has_data': bool(traffic_started_at),
        'traffic_started_at': traffic_started_at.isoformat() if traffic_started_at else None,
        'submissions': submissions,
        'page_ranking_has_data': bool(page_rows),
        'top_pages': page_rows,
        'top_problems': top_problems,
        'verdicts': verdicts,
        'summary': {
            'users': User.objects.count(),
            'problems': Problem.objects.filter(is_public=True).count(),
            'submissions': total_submissions,
            'acceptance_rate': round(accepted * 100 / total_submissions, 1) if total_submissions else 0,
            'database_size': _format_bytes(_database_size_bytes()),
            'health': f'{operational}/{len(components)} 正常' if components else '未配置',
        },
    })


def dashboard_location_mode_view(request):
    if request.method != 'POST':
        return JsonResponse({'detail': 'POST required'}, status=405)
    try:
        payload = json.loads(request.body.decode('utf-8'))
        mode = payload.get('mode')
    except (TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({'detail': 'Invalid location mode'}, status=400)
    if mode not in {'browser', 'ip'}:
        return JsonResponse({'detail': 'Invalid location mode'}, status=400)

    config = SiteConfig.objects.order_by('pk').first()
    if config is None:
        config = SiteConfig(pk=1, browser_geolocation_enabled=mode == 'browser')
        config.save()
    else:
        config.browser_geolocation_enabled = mode == 'browser'
        config.save(update_fields=['browser_geolocation_enabled', 'updated_at'])
    return JsonResponse({'mode': mode})


_original_admin_get_urls = admin.site.get_urls


def _admin_get_urls():
    custom_urls = [
        path(
            'dashboard-metrics/',
            admin.site.admin_view(dashboard_metrics_view),
            name='dashboard_metrics',
        ),
        path(
            'dashboard-location-mode/',
            admin.site.admin_view(dashboard_location_mode_view),
            name='dashboard_location_mode',
        ),
        path(
            'env-generator/',
            admin.site.admin_view(env_generator_view),
            name='env_generator',
        ),
    ]
    return custom_urls + _original_admin_get_urls()


admin.site.get_urls = _admin_get_urls


def _register_singleton_admin(klass, short_description, fieldsets=None, list_display=None):
    """Register a singleton config class as a read/add-one-row admin.

    Implements ``has_add_permission`` and ``has_delete_permission`` so
    that only one row (pk=1) can exist, and exposes the usual list page
    with a "Clear caches" / "Refresh health" action plus a custom view
    button.
    """

    class SingletonAdmin(admin.ModelAdmin):
        def has_add_permission(self, request):
            if klass.objects.exists():
                return False
            return super().has_add_permission(request)

        def has_delete_permission(self, request, obj=None):
            return False

    if fieldsets is not None:
        SingletonAdmin.fieldsets = fieldsets
    if list_display is not None:
        SingletonAdmin.list_display = list_display
    else:
        SingletonAdmin.list_display = ('pk', 'updated_at')
    SingletonAdmin.__name__ = f'{klass.__name__}Admin'
    admin.site.register(klass, SingletonAdmin)
    return SingletonAdmin


class _SingletonAdminMixin:
    def has_add_permission(self, request):
        if self.model.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False


# ---------------------------------------------------------------------
# CacheConfig.
# ---------------------------------------------------------------------
@admin.register(CacheConfig)
class CacheConfigAdmin(_SingletonAdminMixin, admin.ModelAdmin):
    """Cached-data tunables (health-check cache TTL, GitHub cache TTL, page cache)."""

    verbose_name_plural = '缓存配置'
    fieldsets = (
        (
            '缓存配置',
            {'fields': ('health_cache_seconds', 'github_cache_seconds', 'page_cache_seconds'),
             'description': '调整各类缓存的保留时间；修改立即生效，无需重启。'},
        ),
    )
    list_display = (
        'pk',
        'health_cache_seconds',
        'github_cache_seconds',
        'page_cache_seconds',
        'updated_at',
    )
    actions = ['clear_all_caches_action', 'clear_page_cache_action', 'clear_devlog_cache_action']

    @admin.action(description='一键清除所有缓存（页面缓存 + devlog 缓存）')
    def clear_all_caches_action(self, request, queryset):
        from .views import clear_all_caches
        removed = clear_all_caches()
        self.message_user(request, f'已清除所有缓存，共 {removed} 个键')

    @admin.action(description='仅清除页面缓存（cache_page）')
    def clear_page_cache_action(self, request, queryset):
        from .views import clear_page_cache
        removed = clear_page_cache()
        self.message_user(request, f'页面缓存已清除，共 {removed} 个键')

    @admin.action(description='仅清除 devlog 缓存（健康检查 + GitHub 提交）')
    def clear_devlog_cache_action(self, request, queryset):
        from .views import clear_devlog_cache
        removed = clear_devlog_cache()
        self.message_user(request, f'devlog 缓存已清除，共 {removed} 个键')


# ---------------------------------------------------------------------
# HealthCheckConfig.
# ---------------------------------------------------------------------
@admin.register(HealthCheckConfig)
class HealthCheckConfigAdmin(_SingletonAdminMixin, admin.ModelAdmin):
    """Health-check knobs for the status page / auto probes."""

    fieldsets = (
        (
            '滑动窗口与保留',
            {'fields': ('uptime_window_days', 'sample_retention_days', 'file_change_days'),
             'description': '控制健康检查样本的计算窗口与保留时长。'},
        ),
        (
            '评测系统阈值',
            {'fields': ('judge_max_wait_seconds', 'judge_pass_score', 'judge_score_degraded', 'judge_score_partial'),
             'description': '控制评测系统健康检查超时时间与状态判定阈值。'},
        ),
        (
            '告警',
            {'fields': ('health_alert_enabled',),
             'description': '关闭后，自动健康检查将不再向 MANAGERS 发送告警邮件。'},
        ),
    )
    list_display = (
        'pk',
        'uptime_window_days',
        'sample_retention_days',
        'judge_max_wait_seconds',
        'judge_pass_score',
        'health_alert_enabled',
        'updated_at',
    )
    actions = ['refresh_health_checks', 'clear_all_caches']

    @admin.action(description='立即刷新所有健康检查（忽略缓存）')
    def refresh_health_checks(self, request, queryset):
        from .views import _refresh_auto_components
        _refresh_auto_components(force_refresh=True)
        self.message_user(request, '健康检查已刷新')

    @admin.action(description='清除所有缓存（健康检查 + GitHub 提交）')
    def clear_all_caches(self, request, queryset):
        from .views import clear_devlog_cache
        removed = clear_devlog_cache()
        self.message_user(request, f'所有缓存已清除，共清除 {removed} 个缓存键')


# ---------------------------------------------------------------------
# RegistrationConfig.
# ---------------------------------------------------------------------
@admin.register(RegistrationConfig)
class RegistrationConfigAdmin(_SingletonAdminMixin, admin.ModelAdmin):
    """注册流程 feature-gates — "注册需邮箱验证", 验证码 TTL 等。"""

    fieldsets = (
        (
            '注册',
            {'fields': ('registration_enabled', 'email_verification_required'),
             'description': '"注册需邮箱验证"关闭后，注册表单不再要求输入邮箱验证码。'},
        ),
        (
            '邮箱验证码',
            {'fields': ('verification_code_ttl_seconds', 'verification_code_length',
                        'verification_rate_limit_per_hour'),
             'description': '验证码的有效期、长度与发送频率限制。'},
        ),
    )
    list_display = (
        'pk',
        'registration_enabled',
        'email_verification_required',
        'verification_code_ttl_seconds',
        'verification_code_length',
        'verification_rate_limit_per_hour',
        'updated_at',
    )


# ---------------------------------------------------------------------
# CaptchaConfig.
# ---------------------------------------------------------------------

@admin.register(CaptchaConfig)
class CaptchaConfigAdmin(_SingletonAdminMixin, admin.ModelAdmin):
    """图形验证码参数 — 注册是否需要、登录失败阈值、长度 / 有效期 / 频率限制。"""

    fieldsets = (
        (
            '启用 / 禁用',
            {'fields': ('captcha_on_register', 'captcha_require_on_forgot_password',
                        'captcha_require_on_all_post', 'captcha_submission_captcha_enabled'),
             'description': '注册时、找回密码时强制要求图形验证码。'
                            '“所有 POST 操作” 模式用于高风险 / 被攻击时段；'
                            '“提交时频率限制”为用户在单位时间内提交超过阈值后，'
                            '额外要求图形验证码。'},
        ),
        (
            '登录保护',
            {'fields': ('captcha_on_login_after_failures',),
             'description': '同一 IP 登录失败 N 次后强制出示图形验证码。'},
        ),
        (
            '长度 / 有效期 / 频率',
            {'fields': ('captcha_answer_length', 'captcha_challenge_ttl_seconds',
                        'captcha_per_ip_per_minute',
                        'captcha_attempts_per_ip_per_10_minutes'),
             'description': '验证码长度（字符数）、单个验证码有效时长、'
                            '每 IP 每时段生成 / 尝试次数限制。'},
        ),
        (
            '提交频率阈值',
            {'fields': ('captcha_submission_limit', 'captcha_submission_window_minutes'),
             'description': '当用户在 “窗口（分钟）” 时间内累计提交次数超过 “阈值” 时，'
                            '后续提交必须先输入图形验证码。修改后立即生效。'},
        ),
        (
            '头像访问保护',
            {'fields': ('captcha_avatar_captcha_enabled',
                        'captcha_avatar_request_limit',
                        'captcha_avatar_request_window_minutes'),
             'description': '按请求者 IP 统计头像访问频率。达到阈值后，头像请求会要求完成图形验证码。'},
        ),
    )
    list_display = (
        'pk',
        'captcha_on_register',
        'captcha_on_login_after_failures',
        'captcha_answer_length',
        'captcha_challenge_ttl_seconds',
        'captcha_per_ip_per_minute',
        'captcha_attempts_per_ip_per_10_minutes',
        'captcha_require_on_forgot_password',
        'captcha_submission_captcha_enabled',
        'captcha_submission_limit',
        'captcha_submission_window_minutes',
        'captcha_avatar_captcha_enabled',
        'captcha_avatar_request_limit',
        'captcha_avatar_request_window_minutes',
        'updated_at',
    )


# ---------------------------------------------------------------------
# EmailConfig.
# ---------------------------------------------------------------------

@admin.register(EmailConfig)
class EmailConfigAdmin(_SingletonAdminMixin, admin.ModelAdmin):
    """SMTP / 发件人 / 管理员收件人配置 — 保存后立即生效（无需重启）。

    ``email_host_password`` 是 *可选覆盖*：实际密码优先来自
    ``.env`` / 环境变量 ``EMAIL_HOST_PASSWORD``，只有当数据库中
    有非空的值时才会覆盖它。这样敏感凭据只保留在系统环境中。
    """

    fieldsets = (
        (
            '后端 / 服务器',
            {'fields': ('email_backend', 'email_host', 'email_port',
                        'email_use_tls', 'email_use_ssl', 'email_timeout'),
             'description': '常用配置：STARTTLS 端口 587、SSL 端口 465。后端可以填 django 内置的 console / smtp / file / locmem 等完整类路径。'},
        ),
        (
            '账号',
            {'fields': ('email_host_user', 'email_host_password'),
             'description': '邮箱用户名 / 授权码。密码字段 *不* 在列表中展示，'
                            '仅作为管理员可选覆盖。实际密码优先来自 ``.env`` 的 '
                            '``EMAIL_HOST_PASSWORD``。'},
        ),
        (
            '展示 / 收件人',
            {'fields': ('default_from_email', 'site_name_for_email', 'admin_recipients'),
             'description': '默认发件人（如 "Guwu OJ <noreply@your-domain.com>"）、邮件标题中的站点名、以及健康告警的管理员收件人（每行一个邮箱）。'},
        ),
    )
    list_display = (
        'pk',
        'email_host',
        'email_port',
        'email_use_tls',
        'email_use_ssl',
        'email_host_user',
        'default_from_email',
        'email_timeout',
        'updated_at',
    )

    # Password-style widget so the secret is not shown as plain text.
    def get_form(self, request, obj=None, **kwargs):
        from django.forms import PasswordInput
        form_class = super().get_form(request, obj=obj, **kwargs)
        form_class.base_fields['email_host_password'].widget = PasswordInput(
            render_value=False, attrs={'autocomplete': 'new-password'}
        )
        return form_class

    def get_queryset(self, request):
        # Never fetch the password column for the changelist.
        qs = super().get_queryset(request)
        return qs.defer('email_host_password')

    actions = ('send_test_email',)

    @admin.action(description='发送测试邮件（给管理员收件人 + 当前用户邮箱）')
    def send_test_email(self, request, queryset):
        # Always use the singleton row (queryset contains the current selection)
        cfg = queryset.filter(pk=1).first() or EmailConfig.objects.filter(pk=1).first()
        if cfg is None:
            self.message_user(request, '尚未创建邮箱配置，请先添加一行。', level='ERROR')
            return
        try:
            from devlog.email_config_helpers import (
                apply_email_settings, admin_recipient_list, site_name_for_email,
            )
            apply_email_settings()
            site = site_name_for_email()
            recipients = list(admin_recipient_list())
            # Send to the admin user performing the action too, when possible
            user_email = getattr(request.user, 'email', None)
            if user_email and '@' in user_email and user_email not in recipients:
                recipients.append(user_email)
            if not recipients:
                self.message_user(request, '未配置收件人，无法发送测试邮件。', level='ERROR')
                return

            from django.core.mail import EmailMultiAlternatives
            from django.conf import settings
            now = timezone.now().strftime('%Y-%m-%d %H:%M:%S')
            html = f"""<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head><meta http-equiv="Content-Type" content="text/html; charset=utf-8" />
<title>{site} · 测试邮件</title></head>
<body style="margin:0;padding:0;background:#f5f7fb;font-family:-apple-system,Segoe UI,PingFang SC,Microsoft YaHei,Helvetica,Arial,sans-serif;color:#2d3748;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f5f7fb;">
  <tr><td align="center" style="padding:24px 12px;">
    <table width="560" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;width:100%;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 8px 24px rgba(37,99,235,0.12);border:1px solid #dbeafe;">
      <tr><td align="center" style="background:linear-gradient(135deg,#2563eb,#3b82f6);padding:28px 24px;">
        <span style="display:inline-block;background:rgba(255,255,255,0.18);border:1px solid rgba(255,255,255,0.4);color:#ffffff;font-weight:600;padding:6px 14px;border-radius:999px;font-size:12px;">Test Mail · 测试邮件</span>
        <h1 style="margin:14px 0 6px 0;font-size:22px;font-weight:700;color:#ffffff;">✅ {site} SMTP 配置成功</h1>
        <p style="margin:0;color:rgba(255,255,255,0.92);font-size:14px;">如果您看到这封邮件，说明 SMTP / 发件人设置工作正常。</p>
      </td></tr>
      <tr><td style="padding:24px;font-size:15px;color:#2d3748;line-height:1.6;">
        <p><strong>服务器：</strong>{cfg.email_host}:{cfg.email_port}（{'TLS' if cfg.email_use_tls else ''}{' / ' if cfg.email_use_tls and cfg.email_use_ssl else ''}{'SSL' if cfg.email_use_ssl else ''}{'  明文' if not cfg.email_use_tls and not cfg.email_use_ssl else ''}）</p>
        <p><strong>发件用户名：</strong>{cfg.email_host_user or '(空)'}</p>
        <p><strong>默认发件地址：</strong>{cfg.default_from_email or '(空，使用用户名)'}</p>
        <p><strong>发送时间：</strong>{now}</p>
        <p style="margin-top:18px;font-size:12px;color:#9ca3af;">本邮件由 {site} 管理后台“发送测试邮件” 功能自动发出。</p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>
"""
            plain = f"{site} 测试邮件成功。\n\n服务器：{cfg.email_host}:{cfg.email_port}\n用户名：{cfg.email_host_user}\n发送时间：{now}\n"
            from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', None)
            msg = EmailMultiAlternatives(
                subject=f"[{site}] SMTP 测试邮件",
                body=plain,
                from_email=from_email,
                to=recipients,
            )
            msg.attach_alternative(html, 'text/html')
            msg.send(fail_silently=False)
            self.message_user(request, f'测试邮件已发送到: {", ".join(recipients)}')
        except Exception as exc:
            self.message_user(request, f'测试邮件发送失败: {exc}', level='ERROR')


# ---------------------------------------------------------------------
# ServiceComponent + HealthSample + DevLogEntry + FileChange + FileSnapshot.
# ---------------------------------------------------------------------
@admin.register(ServiceComponent)
class ServiceComponentAdmin(admin.ModelAdmin):
    list_display = ('name', 'status', 'uptime_window_display', 'sample_count_display',
                    'order', 'auto_check', 'updated_at')
    list_editable = ('status', 'order', 'auto_check')
    list_filter = ('status', 'auto_check')
    search_fields = ('name', 'name_en')
    actions = ['refresh_health_checks', 'clear_all_caches']

    @staticmethod
    def _window_days():
        try:
            from devlog.models import HealthCheckConfig
            cfg = HealthCheckConfig.objects.first()
            if cfg and cfg.uptime_window_days:
                return int(cfg.uptime_window_days)
        except Exception:
            pass
        return 30

    @admin.display(description='30 天可用率')
    def uptime_window_display(self, obj):
        v = obj.uptime_90_days(window_days=self._window_days())
        if v is None:
            return '—'
        return f'{v:.2f}%'

    @admin.display(description='近 30 天样本数')
    def sample_count_display(self, obj):
        try:
            return obj.sample_count_in_window(window_days=self._window_days())
        except Exception:
            return 0

    @admin.action(description='立即刷新所有健康检查（忽略缓存）')
    def refresh_health_checks(self, request, queryset):
        from .views import _refresh_auto_components
        _refresh_auto_components(force_refresh=True)
        self.message_user(request, '健康检查已刷新')

    @admin.action(description='清除所有缓存（健康检查 + GitHub 提交）')
    def clear_all_caches(self, request, queryset):
        from .views import clear_devlog_cache
        removed = clear_devlog_cache()
        self.message_user(request, f'所有缓存已清除，共清除 {removed} 个缓存键')


@admin.register(HealthSample)
class HealthSampleAdmin(admin.ModelAdmin):
    list_display = ('component', 'ok_display', 'score', 'sampled_at')
    list_filter = ('component', 'ok', 'sampled_at')
    search_fields = ('component__name', 'component__name_en')
    readonly_fields = ('component', 'ok', 'score', 'sampled_at')
    ordering = ['-sampled_at']

    @admin.display(description='是否通过')
    def ok_display(self, obj):
        return {True: 'OK', False: 'FAIL', None: 'UNKNOWN'}.get(obj.ok)


@admin.register(DevLogEntry)
class DevLogEntryAdmin(admin.ModelAdmin):
    list_display = ('title', 'version', 'pinned', 'author', 'created_at')
    list_filter = ('pinned', 'created_at')
    search_fields = ('title', 'version', 'body')
    autocomplete_fields = ('author',)
    fieldsets = (
        (None, {
            'fields': ('title', 'version', 'pinned', 'author'),
        }),
        ('内容（Markdown）', {
            'fields': ('body', 'body_preview'),
            'description': '在 body 中输入 Markdown 文本，下方会显示渲染预览。',
        }),
    )
    readonly_fields = ('body_preview',)

    def body_preview(self, obj):
        from problems.markdown_utils import render_markdown
        from django.utils.html import format_html
        html = render_markdown(obj.body or '')
        if not html:
            return '(内容为空)'
        return format_html(
            '<div style="border:1px solid #dee2e6;border-radius:6px;padding:12px;'
            'background:#f8f9fa;margin-top:8px;">{}</div>',
            html
        )
    body_preview.short_description = 'Markdown 预览（只读）'


@admin.register(FileChange)
class FileChangeAdmin(admin.ModelAdmin):
    list_display = ('path', 'change_type', 'detected_at', 'remarks', 'annotated_by')
    list_filter = ('change_type', 'detected_at')
    search_fields = ('path', 'remarks', 'description')
    readonly_fields = ('path', 'change_type', 'file_hash', 'old_hash', 'size', 'detected_at', 'description_preview')
    fieldsets = (
        (None, {
            'fields': ('path', 'change_type', 'detected_at', 'file_hash', 'old_hash', 'size'),
        }),
        ('标注（支持 Markdown）', {
            'fields': ('annotated_by', 'remarks', 'description', 'description_preview'),
            'description': '在 description 中输入 Markdown 文本，下方会显示渲染预览。',
        }),
    )

    def description_preview(self, obj):
        from problems.markdown_utils import render_markdown
        from django.utils.html import format_html
        html = render_markdown(obj.description or '')
        if not html:
            return '(内容为空)'
        return format_html(
            '<div style="border:1px solid #dee2e6;border-radius:6px;padding:12px;'
            'background:#f8f9fa;margin-top:8px;">{}</div>',
            html
        )
    description_preview.short_description = 'Markdown 预览（只读）'


@admin.register(FileSnapshot)
class FileSnapshotAdmin(admin.ModelAdmin):
    list_display = ('path', 'size', 'updated_at')
    search_fields = ('path',)


@admin.register(SiteConfig)
class SiteConfigAdmin(_SingletonAdminMixin, admin.ModelAdmin):
    """前端资源 / Monaco + Bootstrap CDN / 静态文件缓存相关配置（singleton）。"""

    fieldsets = (
        (
            'Monaco 代码编辑器',
            {'fields': ('monaco_source', 'monaco_custom_base', 'monaco_version'),
             'description': '切换提交页代码编辑器的 CDN 源；'
                            '若默认源访问较慢，可切换至其他 CDN，或在下方填入自建 CDN 的完整 URL。'
                            '修改后，下一次打开提交页面即生效。'},
        ),
        (
            'Bootstrap 与图标',
            {'fields': (
                'bootstrap_source',
                'bootstrap_css_version',
                'bootstrap_icons_version',
                'bootstrap_custom_css',
                'bootstrap_custom_icons',
                'bootstrap_custom_js',
            ),
             'description': '切换全站 Bootstrap CSS / 图标字体 / JS bundle 的加载源。'
                            '选择"自定义地址"时，需要在下方同时填写 CSS / 图标 / JS 三条 URL。'},
        ),
        (
            '静态文件缓存',
            {'fields': ('static_cache_ttl_seconds',),
             'description': '控制 /static/ 目录下静态文件（css / js / 图片 / Monaco / bootstrap）的 '
                            '浏览器 Cache-Control: max-age 秒数。默认 86400（1 天）；'
                            '带有内容 hash 的文件会自动提升为 1 年 immutable。修改后约 30 秒生效。'},
        ),
        (
            '数据库备份',
            {'fields': ('database_backup_dir',),
             'description': '通过 HTTP（非 HTTPS）访问时，“备份当前数据库”会把备份文件写入该目录，'
                            '“从备份导入”也从该目录列出候选文件。'},
        ),
    )
    list_display = (
        'pk',
        'monaco_source',
        'bootstrap_source',
        'static_cache_ttl_seconds',
        'updated_at',
    )
    actions = []
    change_list_template = 'admin/devlog/siteconfig/change_list.html'

    # ------------------------------------------------------------------
    # 数据库备份 / 导入快捷操作
    # ------------------------------------------------------------------
    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                'database-backup/',
                self.admin_site.admin_view(self.database_backup_view),
                name='devlog_siteconfig_database_backup',
            ),
            path(
                'database-restore/',
                self.admin_site.admin_view(self.database_restore_view),
                name='devlog_siteconfig_database_restore',
            ),
        ]
        return custom + urls

    def _require_backup_permission(self, request):
        """Backup and restore expose/replace the entire database.

        ``admin_view`` only checks ``is_staff``, and even ``change`` permission
        on SiteConfig is far weaker than "may read every row of every table",
        so restrict both operations to superusers.
        """
        if not request.user.is_superuser:
            raise PermissionDenied

    def _backup_directory(self, request):
        from .dbbackup import BackupError

        config = SiteConfig.objects.order_by('pk').first()
        directory = (
            config.backup_directory() if config is not None
            else Path(settings.BASE_DIR) / 'backups' / 'database'
        )
        if not directory.is_absolute():
            raise BackupError(f'备份目录必须是绝对路径，当前配置为 {directory}。')
        return directory

    def database_backup_view(self, request):
        from . import dbbackup

        self._require_backup_permission(request)
        redirect_url = reverse('admin:devlog_siteconfig_changelist')

        if request.method != 'POST':
            # Creating a dump is a side effect, so a bare GET only renders the
            # confirmation form. SimpleUI rebuilds the changelist object-tools
            # from ``li a`` elements and drops anything else, so the shortcut
            # has to be a plain link into this page rather than a POST button.
            directory_error = ''
            backup_dir = ''
            if not request.is_secure():
                try:
                    backup_dir = str(self._backup_directory(request))
                except dbbackup.BackupError as exc:
                    directory_error = str(exc)
            context = {
                **self.admin_site.each_context(request),
                'opts': self.model._meta,
                'title': '备份当前数据库',
                'is_secure': request.is_secure(),
                'backup_dir': backup_dir,
                'directory_error': directory_error,
                'example_filename': dbbackup.suggested_filename(),
            }
            return render(
                request,
                'admin/devlog/siteconfig/database_backup.html',
                context,
            )

        try:
            if request.is_secure():
                # HTTPS: stream the dump to the browser and keep nothing behind.
                path_obj = dbbackup.temporary_backup()
                response = FileResponse(
                    open(path_obj, 'rb'),
                    as_attachment=True,
                    filename=path_obj.name,
                    content_type='application/octet-stream',
                )
                response['Cache-Control'] = 'no-store'
                # The temporary directory is removed once the response body has
                # been fully written out.
                response._resource_closers.append(
                    lambda: shutil.rmtree(path_obj.parent, ignore_errors=True)
                )
                return response

            directory = self._backup_directory(request)
            written = dbbackup.create_backup(directory / dbbackup.suggested_filename())
            messages.success(
                request,
                f'当前为非 HTTPS 访问，备份已保存到服务器路径：{written}'
                f'（{dbbackup.format_size(written.stat().st_size)}）。',
            )
        except dbbackup.BackupError as exc:
            messages.error(request, f'备份失败：{exc}')
        return redirect(redirect_url)

    def database_restore_view(self, request):
        from . import dbbackup

        self._require_backup_permission(request)
        redirect_url = reverse('admin:devlog_siteconfig_changelist')
        upload_allowed = request.is_secure()

        directory = None
        backups = []
        directory_error = None
        try:
            directory = self._backup_directory(request)
            backups = dbbackup.list_backups(directory)
        except dbbackup.BackupError as exc:
            directory_error = str(exc)

        if request.method == 'POST':
            source = request.POST.get('source') or 'path'
            staged = None
            try:
                if not request.POST.get('confirm'):
                    raise dbbackup.BackupError('请先勾选确认框，导入会覆盖当前数据库的全部数据。')
                if source == 'upload':
                    if not upload_allowed:
                        raise dbbackup.BackupError(
                            '当前为非 HTTPS 访问，已禁用上传文件导入；请改用服务器路径导入。'
                        )
                    upload = request.FILES.get('file')
                    if upload is None:
                        raise dbbackup.BackupError('请选择要上传的备份文件。')
                    staged = dbbackup.stage_upload(upload)
                    dbbackup.restore_backup(staged)
                    messages.success(
                        request, f'已从上传文件 {upload.name} 导入数据库。请重新登录以确认状态。'
                    )
                else:
                    if directory is None:
                        raise dbbackup.BackupError(directory_error or '备份目录不可用。')
                    name = (request.POST.get('filename') or '').strip()
                    if not name:
                        raise dbbackup.BackupError('请选择要导入的服务器备份文件。')
                    chosen = dbbackup.resolve_inside(directory, name)
                    dbbackup.restore_backup(chosen)
                    messages.success(
                        request, f'已从服务器备份 {chosen.name} 导入数据库。请重新登录以确认状态。'
                    )
                return redirect(redirect_url)
            except dbbackup.BackupError as exc:
                messages.error(request, f'导入失败：{exc}')
            finally:
                if staged is not None:
                    shutil.rmtree(staged.parent, ignore_errors=True)

        context = {
            **self.admin_site.each_context(request),
            'title': '从备份导入数据库',
            'opts': self.model._meta,
            'backup_dir': str(directory) if directory else '',
            'directory_error': directory_error,
            'backups': [
                {**item, 'size_display': dbbackup.format_size(item['size'])}
                for item in backups
            ],
            'upload_allowed': upload_allowed,
            'allowed_suffixes': '、'.join(dbbackup.allowed_suffixes()),
            'accept_attr': ','.join(dbbackup.allowed_suffixes()),
            'max_upload_mb': dbbackup.MAX_UPLOAD_BYTES // (1024 * 1024),
        }
        return render(request, 'admin/devlog/siteconfig/database_restore.html', context)
