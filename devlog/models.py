from django.conf import settings
from django.db import models
from django.utils import timezone


class TrafficDailyMetric(models.Model):
    """Anonymous daily public-page view counter used by the admin dashboard."""

    day = models.DateField(unique=True, db_index=True, verbose_name='日期')
    page_views = models.PositiveBigIntegerField(default=0, verbose_name='页面访问量')

    class Meta:
        ordering = ['-day']
        verbose_name = '每日流量'
        verbose_name_plural = '每日流量'

    def __str__(self):
        return f'{self.day}: {self.page_views}'


class TrafficPageMetric(models.Model):
    """Anonymous daily aggregate for a normalized public route."""

    day = models.DateField(db_index=True, verbose_name='日期')
    path = models.CharField(max_length=200, verbose_name='页面路径')
    page_views = models.PositiveBigIntegerField(default=0, verbose_name='页面访问量')

    class Meta:
        ordering = ['-day', 'path']
        verbose_name = '每日页面流量'
        verbose_name_plural = '每日页面流量'
        constraints = [
            models.UniqueConstraint(fields=['day', 'path'], name='unique_traffic_page_day'),
        ]
        indexes = [
            models.Index(fields=['day', 'path']),
        ]

    def __str__(self):
        return f'{self.day} {self.path}: {self.page_views}'


class TrafficCountryMetric(models.Model):
    """Anonymous daily request aggregate for a GeoIP-resolved country."""

    day = models.DateField(db_index=True, verbose_name='日期')
    country_code = models.CharField(max_length=2, verbose_name='国家代码')
    country_name = models.CharField(max_length=100, verbose_name='国家')
    latitude = models.FloatField(verbose_name='纬度')
    longitude = models.FloatField(verbose_name='经度')
    requests = models.PositiveBigIntegerField(default=0, verbose_name='请求数')

    class Meta:
        ordering = ['-day', '-requests', 'country_code']
        verbose_name = '每日国家流量'
        verbose_name_plural = '每日国家流量'
        constraints = [
            models.UniqueConstraint(fields=['day', 'country_code'], name='unique_traffic_country_day'),
        ]
        indexes = [models.Index(fields=['day', 'country_code'])]

    def __str__(self):
        return f'{self.day} {self.country_name}: {self.requests}'
class UserTrafficMetric(models.Model):
    """Hourly normalized-route browsing count for a consented visitor."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.CASCADE, related_name='traffic_metrics',
        verbose_name='用户',
    )
    session_key = models.CharField('匿名会话', max_length=40, null=True, blank=True)
    hour = models.DateTimeField('小时', db_index=True)
    path = models.CharField('页面路径', max_length=200)
    page_views = models.PositiveBigIntegerField('访问次数', default=0)

    class Meta:
        verbose_name = '用户浏览流量'
        verbose_name_plural = '用户浏览流量'
        indexes = [
            models.Index(fields=['user', 'hour']),
            models.Index(fields=['session_key', 'hour']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'hour', 'path'], condition=models.Q(user__isnull=False),
                name='unique_user_traffic_hour_path',
            ),
            models.UniqueConstraint(
                fields=['session_key', 'hour', 'path'], condition=models.Q(session_key__isnull=False),
                name='unique_session_traffic_hour_path',
            ),
        ]

    def __str__(self):
        owner = self.user or self.session_key or 'anonymous'
        return f'{owner} {self.hour} {self.path}: {self.page_views}'


class ServiceComponent(models.Model):
    """A single service component shown on the status page (left column)."""

    STATUS_OPERATIONAL = 'operational'
    STATUS_DEGRADED = 'degraded'
    STATUS_PARTIAL = 'partial'
    STATUS_MAJOR = 'major'
    STATUS_MAINTENANCE = 'maintenance'

    STATUS_CHOICES = [
        (STATUS_OPERATIONAL, '正常运作'),
        (STATUS_DEGRADED, '性能下降'),
        (STATUS_PARTIAL, '部分中断'),
        (STATUS_MAJOR, '重大中断'),
        (STATUS_MAINTENANCE, '维护中'),
    ]

    # Higher number == more severe. Used to compute the overall banner.
    STATUS_SEVERITY = {
        STATUS_OPERATIONAL: 0,
        STATUS_MAINTENANCE: 1,
        STATUS_DEGRADED: 2,
        STATUS_PARTIAL: 3,
        STATUS_MAJOR: 4,
    }

    # Backwards-compatible default used by ``uptime_window`` and the pruning
    # threshold when no explicit configuration exists in ``HealthCheckConfig``.
    DEFAULT_WINDOW_DAYS = 30

    name = models.CharField('名称', max_length=100)
    name_en = models.CharField('英文名', max_length=100, blank=True)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default=STATUS_OPERATIONAL)
    description = models.CharField('说明', max_length=255, blank=True)
    # Kept for backward compatibility. The template now prefers ``uptime_90_days``
    # which is computed from HealthSample rows over the configured window.
    uptime = models.FloatField('可用率(%)', default=100.0, blank=True)
    order = models.IntegerField('排序', default=0)
    # When set, the status is refreshed automatically from the internal health check.
    auto_check = models.BooleanField('自动健康检查', default=False)
    health_key = models.CharField(
        '健康检查项', max_length=50, blank=True,
        help_text="对应 /health/ 返回的 checks 键，如 database / redis / cache",
    )
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        ordering = ['order', 'id']
        verbose_name = '服务组件'
        verbose_name_plural = '服务组件'

    def __str__(self):
        return self.name

    @property
    def severity(self):
        return self.STATUS_SEVERITY.get(self.status, 0)

    def uptime_90_days(self, window_days=None):
        """Ratio of healthy samples over the last ``window_days`` days.

        If ``window_days`` is None, the configured
        ``HealthCheckConfig.uptime_window_days`` is used (default 30).
        A "healthy" sample has ``HealthSample.ok=True``. Returns ``None``
        when there are zero samples in the window so the template can
        fall back to a legacy 100% display or similar.
        """
        if window_days is None:
            try:
                from devlog.models import HealthCheckConfig as _HC
                _cfg = _HC.objects.first()
                if _cfg and _cfg.uptime_window_days:
                    window_days = int(_cfg.uptime_window_days)
            except Exception:
                window_days = None
        if not window_days:
            window_days = self.DEFAULT_WINDOW_DAYS
        cutoff = timezone.now() - timezone.timedelta(days=window_days)
        try:
            total = self.health_samples.filter(sampled_at__gte=cutoff).count()
        except Exception:
            total = 0
        if total == 0:
            return None
        try:
            healthy = self.health_samples.filter(sampled_at__gte=cutoff, ok=True).count()
        except Exception:
            healthy = 0
        return round(healthy * 100.0 / total, 2)

    def sample_count_in_window(self, window_days=None):
        """Number of samples in the configured uptime window."""
        if window_days is None:
            try:
                from devlog.models import HealthCheckConfig as _HC
                _cfg = _HC.objects.first()
                if _cfg and _cfg.uptime_window_days:
                    window_days = int(_cfg.uptime_window_days)
            except Exception:
                window_days = None
        if not window_days:
            window_days = self.DEFAULT_WINDOW_DAYS
        cutoff = timezone.now() - timezone.timedelta(days=window_days)
        try:
            return self.health_samples.filter(sampled_at__gte=cutoff).count()
        except Exception:
            return 0

    @property
    def badge_class(self):
        return {
            self.STATUS_OPERATIONAL: 'success',
            self.STATUS_DEGRADED: 'warning',
            self.STATUS_PARTIAL: 'warning',
            self.STATUS_MAJOR: 'danger',
            self.STATUS_MAINTENANCE: 'info',
        }.get(self.status, 'secondary')


class DevLogEntry(models.Model):
    """A manually authored changelog / developer log entry (right-of-status panel)."""

    version = models.CharField('版本号', max_length=40, blank=True)
    title = models.CharField('标题', max_length=200)
    body = models.TextField('内容(Markdown)', blank=True)
    pinned = models.BooleanField('置顶', default=False)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='devlog_entries', verbose_name='作者',
    )
    created_at = models.DateTimeField('创建时间', default=timezone.now)

    class Meta:
        ordering = ['-pinned', '-created_at']
        verbose_name = '开发者日志'
        verbose_name_plural = '开发者日志'

    def __str__(self):
        return self.title


class FileSnapshot(models.Model):
    """Baseline hash of a tracked project file, used to detect future changes."""

    path = models.CharField(max_length=500, unique=True)
    file_hash = models.CharField(max_length=64)
    size = models.BigIntegerField(default=0)
    mtime = models.FloatField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['path']
        verbose_name = '文件快照'
        verbose_name_plural = '文件快照'

    def __str__(self):
        return self.path


class FileChange(models.Model):
    """A detected change to a project file. Users can annotate it with remarks."""

    CHANGE_ADDED = 'added'
    CHANGE_MODIFIED = 'modified'
    CHANGE_DELETED = 'deleted'
    CHANGE_CHOICES = [
        (CHANGE_ADDED, '新增'),
        (CHANGE_MODIFIED, '修改'),
        (CHANGE_DELETED, '删除'),
    ]

    path = models.CharField('文件路径', max_length=500)
    change_type = models.CharField('变更类型', max_length=10, choices=CHANGE_CHOICES)
    file_hash = models.CharField(max_length=64, blank=True)
    old_hash = models.CharField(max_length=64, blank=True)
    size = models.BigIntegerField('大小(字节)', default=0)
    detected_at = models.DateTimeField('检测时间', default=timezone.now)
    # User editable fields:
    remarks = models.CharField('备注', max_length=255, blank=True)
    description = models.TextField('描述', blank=True)
    annotated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='annotated_changes', verbose_name='备注人',
    )

    class Meta:
        ordering = ['-detected_at', 'path']
        verbose_name = '文件改动'
        verbose_name_plural = '文件改动'
        indexes = [
            models.Index(fields=['-detected_at']),
        ]

    def __str__(self):
        return f'[{self.get_change_type_display()}] {self.path}'

    @property
    def badge_class(self):
        return {
            self.CHANGE_ADDED: 'success',
            self.CHANGE_MODIFIED: 'primary',
            self.CHANGE_DELETED: 'danger',
        }.get(self.change_type, 'secondary')


class HealthSample(models.Model):
    """A single binary (healthy / unhealthy) sample of a service component.

    ``ServiceComponent.uptime_90_days`` is computed from these samples.
    One row is generated by :func:`devlog.views._do_refresh_auto_components`
    every time the health-check job runs (30 min cron / admin "Refresh health
    checks" / status page view). Old rows beyond ``UPTO90_DAYS + 7`` are pruned
    automatically so the table does not grow unbounded.
    """

    component = models.ForeignKey(
        ServiceComponent,
        on_delete=models.CASCADE,
        related_name='health_samples',
        verbose_name='服务组件',
    )
    # True = 该次采样组件通过检查; False / None = 失败 / 未知
    ok = models.BooleanField('是否通过', null=True, default=False)
    # Optional numeric score — currently used by the judge component
    # (number of passing test cases for the A+B health problem).
    score = models.IntegerField('评分 / 通过用例数', default=0)
    sampled_at = models.DateTimeField('采样时间', default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-sampled_at']
        verbose_name = '健康检查采样'
        verbose_name_plural = '健康检查采样'
        indexes = [
            models.Index(fields=['component', '-sampled_at']),
        ]

    def __str__(self):
        ok_str = {True: 'OK', False: 'FAIL', None: 'UNKNOWN'}.get(self.ok)
        return f"{self.component.name} [{ok_str}] score={self.score} {self.sampled_at:%Y-%m-%d %H:%M}"


class _Singleton(models.Model):
    """Shared behaviour for every singleton config model:

    - ``pk`` is fixed at ``1`` so the rest of the codebase can refer to
      it as a singleton;
    - ``updated_at`` tracks the last save time;
    - default ``__str__`` reports the class name and the save time.
    """

    updated_at = models.DateTimeField('最后更新时间', auto_now=True)

    class Meta:
        abstract = True

    def __str__(self):
        return f'{self.__class__.__name__} (pk={self.pk}, updated {self.updated_at:%Y-%m-%d %H:%M})'


class CacheConfig(_Singleton):
    """缓存相关的可调参数（singleton）。"""

    health_cache_seconds = models.IntegerField(
        '健康检查结果缓存(秒)', default=1800,
        help_text='status_page 读取健康检查结果时的缓存 TTL',
    )
    github_cache_seconds = models.IntegerField(
        'GitHub 提交记录缓存(秒)', default=7200,
        help_text='GitHub 提交列表在缓存中保留的秒数',
    )
    page_cache_seconds = models.IntegerField(
        '页面缓存默认 TTL(秒)', default=900,
        help_text='整页 cache_page 的默认保留时间',
    )

    class Meta:
        verbose_name = '缓存配置'
        verbose_name_plural = '缓存配置'


class CaptchaConfig(_Singleton):
    """图形验证码相关的可调参数（singleton）。"""

    captcha_on_register = models.BooleanField('注册时要求验证码', default=True)
    captcha_on_login_after_failures = models.PositiveSmallIntegerField(
        '登录失败次数后要求验证码', default=1,
        help_text='同一 IP 登录失败 N 次后强制要求图形验证码',
    )
    captcha_answer_length = models.PositiveSmallIntegerField(
        '验证码长度', default=5
    )
    captcha_challenge_ttl_seconds = models.IntegerField(
        '验证码有效时间(秒)', default=600
    )
    captcha_per_ip_per_minute = models.PositiveSmallIntegerField(
        '每 IP 每分钟最多生成次数', default=20
    )
    captcha_attempts_per_ip_per_10_minutes = models.PositiveSmallIntegerField(
        '每 IP 每 10 分钟最多验证次数', default=30
    )
    captcha_require_on_forgot_password = models.BooleanField(
        '忘记密码时要求验证码', default=True
    )
    captcha_require_on_all_post = models.BooleanField(
        '所有 POST 操作都要求验证码', default=False,
        help_text='开启后，发帖 / 评论 / 提交题目等所有 POST 请求都会检查是否携带 captcha_id/answer；用于高风险时期',
    )
    captcha_submission_captcha_enabled = models.BooleanField(
        '高频提交时要求验证码', default=True,
        help_text='当用户在时间窗口内提交超过阈值时，必须先输入图形验证码才能继续提交',
    )
    captcha_submission_limit = models.PositiveSmallIntegerField(
        '验证码触发阈值（次数）', default=30,
        help_text='在时间窗口内提交次数超过该值后强制要求图形验证码',
    )
    captcha_submission_window_minutes = models.PositiveSmallIntegerField(
        '验证码触发窗口（分钟）', default=60,
        help_text='统计提交频率的时间窗口长度，修改后立即生效。默认“每小时 30 次提交”后开始要求验证码。',
    )
    captcha_avatar_captcha_enabled = models.BooleanField(
        '头像高频访问时要求验证码', default=True,
        help_text='当同一 IP 在时间窗口内请求头像达到阈值时，要求完成图形验证码。',
    )
    captcha_avatar_request_limit = models.PositiveSmallIntegerField(
        '头像验证码触发阈值（次数）', default=30,
        help_text='同一 IP 在窗口内请求头像达到该次数后要求图形验证码。',
    )
    captcha_avatar_request_window_minutes = models.PositiveSmallIntegerField(
        '头像验证码触发窗口（分钟）', default=1,
        help_text='统计同一 IP 头像请求次数的时间窗口长度。',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '验证码配置'
        verbose_name_plural = '验证码配置'


class EmailConfig(_Singleton):
    """SMTP / 发件配置（singleton）。

    字段基本对应 Django 的 EMAIL_* setting。保存时会通过
    :func:`apply_email_settings` 即时应用到当前 Django 进程，
    因此修改配置后无需重启 gunicorn 即可生效。
    """

    email_backend = models.CharField(
        'Email Backend', max_length=120,
        default='django.core.mail.backends.smtp.EmailBackend',
        help_text='生产环境用 smtp；调试可用 django.core.mail.backends.console.EmailBackend 把邮件打印到日志',
    )
    email_host = models.CharField(
        'SMTP 服务器地址', max_length=120, default='smtp.example.com',
    )
    email_port = models.PositiveIntegerField(
        'SMTP 端口', default=587,
        help_text='TLS 常用 587；SSL 常用 465；未加密常用 25',
    )
    email_use_tls = models.BooleanField('使用 STARTTLS (端口 587)', default=True)
    email_use_ssl = models.BooleanField('使用 SSL (端口 465)', default=False)
    email_host_user = models.CharField(
        'SMTP 用户名', max_length=120, blank=True, default='',
        help_text='例如 noreply@your-domain.com',
    )
    email_host_password = models.CharField(
        'SMTP 密码 / 授权码', max_length=200, blank=True, default='',
        help_text='QQ/163 等邮箱通常使用“授权码”而不是登录密码',
    )
    default_from_email = models.CharField(
        '默认发件人', max_length=200, blank=True, default='',
        help_text='例如 "Guwu OJ <noreply@your-domain.com>"。留空则使用 SMTP 用户名',
    )
    email_timeout = models.PositiveSmallIntegerField(
        '单次发送超时(秒)', default=20,
    )
    admin_recipients = models.TextField(
        '管理员收件人列表', blank=True, default='',
        help_text='健康告警等系统邮件的收件人，每行一个邮箱',
    )
    site_name_for_email = models.CharField(
        '邮件中显示的站点名', max_length=80, blank=True, default='Guwu Online Judge',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '邮箱配置'
        verbose_name_plural = '邮箱配置'


class HealthCheckConfig(_Singleton):
    """健康检查与可用性相关参数（singleton）。

    控制：可用率滑动窗口、样本保留期、评测系统阈值、健康告警开关。
    """

    uptime_window_days = models.IntegerField(
        '可用率滑动窗口(天)', default=30,
        help_text='ServiceComponent.uptime_90_days 使用的窗口大小。',
    )
    sample_retention_days = models.IntegerField(
        '健康检查样本保留天数', default=uptime_window_days.default + 7,
        help_text='超过该天数的 HealthSample 行将在每次健康检查后被清理。',
    )
    judge_max_wait_seconds = models.IntegerField(
        '评测系统探测最长等待(秒)', default=15,
        help_text='提交 A+B 健康用例后最多等待的秒数；超时视为该样本失败。',
    )
    judge_pass_score = models.IntegerField(
        '评测系统健康阈值(通过用例数)', default=3,
        help_text='健康用例通过数 ≥ 该值时该次 sample 记为 OK。',
    )
    judge_score_degraded = models.IntegerField(
        '评测系统状态-性能下降阈值', default=2,
        help_text='score == 该值 时组件状态显示为"性能下降"。',
    )
    judge_score_partial = models.IntegerField(
        '评测系统状态-部分中断阈值', default=1,
        help_text='score == 该值 时组件状态显示为"部分中断"。',
    )
    file_change_days = models.IntegerField(
        '文件变更展示窗口(天)', default=30,
        help_text='status_page 变更列表中仅展示最近 N 天内的变更。',
    )
    health_alert_enabled = models.BooleanField(
        '启用健康告警邮件', default=True,
        help_text='关闭后 _send_health_alert 不发送任何邮件，但仍记录 sample。',
    )

    class Meta:
        verbose_name = '健康检查配置'
        verbose_name_plural = '健康检查配置'


class RegistrationConfig(_Singleton):
    """注册/登录相关的可调参数（singleton）。

    控制：注册是否需要邮箱验证码、验证码有效期/长度、每个邮箱每小时最多请求次数等。
    """

    email_verification_required = models.BooleanField(
        '注册需邮箱验证', default=True,
        help_text='关闭后，注册表单不再要求填写 6 位邮箱验证码。',
    )
    verification_code_ttl_seconds = models.IntegerField(
        '验证码有效期(秒)', default=600,
        help_text='邮箱验证码在 Redis 中的 TTL。默认 10 分钟。',
    )
    verification_code_length = models.IntegerField(
        '验证码长度', default=6,
        help_text='发送给用户的邮箱验证码位数，应与表单中 1–1 对应。',
    )
    verification_rate_limit_per_hour = models.IntegerField(
        '每邮箱每小时最多发送次数', default=5,
        help_text='单个邮箱每小时最多可请求发送验证码的次数。',
    )
    registration_enabled = models.BooleanField(
        '允许注册', default=True,
        help_text='关闭后，全站关闭所有用户注册入口。',
    )

    class Meta:
        verbose_name = '注册配置'
        verbose_name_plural = '注册配置'


class SiteConfig(_Singleton):
    """站点级别的"外观 / 前端资源"参数（singleton）。"""

    MONACO_JSDELIVR = 'jsdelivr'
    MONACO_UNPKG = 'unpkg'
    MONACO_CDNJS = 'cdnjs'
    MONACO_BOOTCDN = 'bootcdn'
    MONACO_CUSTOM = 'custom'

    MONACO_SOURCE_CHOICES = (
        (MONACO_JSDELIVR, 'jsDelivr CDN（默认，全球较快）'),
        (MONACO_UNPKG, 'unpkg CDN（npm 直连）'),
        (MONACO_CDNJS, 'cdnjs CDN'),
        (MONACO_BOOTCDN, 'BootCDN（国内较快）'),
        (MONACO_CUSTOM, '自定义地址（下方填写完整 URL 前缀）'),
    )

    _SOURCE_TEMPLATES = {
        MONACO_JSDELIVR: 'https://cdn.jsdelivr.net/npm/monaco-editor@{version}/min/vs',
        MONACO_UNPKG: 'https://unpkg.com/monaco-editor@{version}/min/vs',
        MONACO_CDNJS: 'https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/{version}/min/vs',
        MONACO_BOOTCDN: 'https://cdn.bootcdn.net/ajax/libs/monaco-editor/{version}/min/vs',
        MONACO_CUSTOM: None,
    }

    monaco_source = models.CharField(
        'Monaco 加载源', max_length=20, default=MONACO_JSDELIVR,
        choices=MONACO_SOURCE_CHOICES,
        help_text='切换提交页代码编辑器的 CDN 源；修改后立即生效（下一次打开页面）',
    )
    monaco_custom_base = models.CharField(
        '自定义 Monaco 基础 URL（仅当上方选择"自定义地址"时使用）',
        max_length=500, blank=True, default='',
        help_text='指向 /min/vs 目录（不含结尾斜杠）',
    )
    monaco_version = models.CharField(
        'Monaco 版本号', max_length=30, default='0.52.2',
        help_text='一般保持默认即可；修改前请确认目标 CDN 存在该版本',
    )

    # ---------- Bootstrap 加载源（CSS + 图标 + JS bundle） ----------
    BOOTSTRAP_JSDELIVR = 'jsdelivr'
    BOOTSTRAP_UNPKG = 'unpkg'
    BOOTSTRAP_CDNJS = 'cdnjs'
    BOOTSTRAP_BOOTCDN = 'bootcdn'
    BOOTSTRAP_CUSTOM = 'custom'

    BOOTSTRAP_SOURCE_CHOICES = (
        (BOOTSTRAP_JSDELIVR, 'jsDelivr CDN（默认，全球较快）'),
        (BOOTSTRAP_UNPKG, 'unpkg CDN（npm 直连）'),
        (BOOTSTRAP_CDNJS, 'cdnjs CDN'),
        (BOOTSTRAP_BOOTCDN, 'BootCDN（国内较快）'),
        (BOOTSTRAP_CUSTOM, '自定义地址（下方填写）'),
    )

    # 对每个源提供 (css, icons, js_bundle) 三条 URL 模板。
    # {css_version} 为 bootstrap 主版本；{icons_version} 为 bootstrap-icons 版本。
    _BOOTSTRAP_SOURCE_TEMPLATES = {
        BOOTSTRAP_JSDELIVR: (
            'https://cdn.jsdelivr.net/npm/bootstrap@{css_version}/dist/css/bootstrap.min.css',
            'https://cdn.jsdelivr.net/npm/bootstrap-icons@{icons_version}/font/bootstrap-icons.css',
            'https://cdn.jsdelivr.net/npm/bootstrap@{css_version}/dist/js/bootstrap.bundle.min.js',
        ),
        BOOTSTRAP_UNPKG: (
            'https://unpkg.com/bootstrap@{css_version}/dist/css/bootstrap.min.css',
            'https://unpkg.com/bootstrap-icons@{icons_version}/font/bootstrap-icons.css',
            'https://unpkg.com/bootstrap@{css_version}/dist/js/bootstrap.bundle.min.js',
        ),
        BOOTSTRAP_CDNJS: (
            'https://cdnjs.cloudflare.com/ajax/libs/bootstrap/{css_version}/css/bootstrap.min.css',
            'https://cdnjs.cloudflare.com/ajax/libs/bootstrap-icons/{icons_version}/font/bootstrap-icons.css',
            'https://cdnjs.cloudflare.com/ajax/libs/bootstrap/{css_version}/js/bootstrap.bundle.min.js',
        ),
        BOOTSTRAP_BOOTCDN: (
            'https://cdn.bootcdn.net/ajax/libs/bootstrap/{css_version}/css/bootstrap.min.css',
            'https://cdn.bootcdn.net/ajax/libs/bootstrap-icons/{icons_version}/font/bootstrap-icons.css',
            'https://cdn.bootcdn.net/ajax/libs/bootstrap/{css_version}/js/bootstrap.bundle.min.js',
        ),
        BOOTSTRAP_CUSTOM: None,
    }

    bootstrap_source = models.CharField(
        'Bootstrap 加载源', max_length=20, default=BOOTSTRAP_JSDELIVR,
        choices=BOOTSTRAP_SOURCE_CHOICES,
        help_text='切换前端页面（提交页/主页等）使用的 Bootstrap 源',
    )
    bootstrap_css_version = models.CharField(
        'Bootstrap 主版本号', max_length=30, default='5.3.0',
        help_text='例如 5.3.0；修改前请确认目标 CDN 存在该版本',
    )
    bootstrap_icons_version = models.CharField(
        'Bootstrap Icons 版本号', max_length=30, default='1.10.0',
        help_text='例如 1.10.0',
    )
    bootstrap_custom_css = models.CharField(
        '自定义 Bootstrap CSS URL', max_length=500, blank=True, default='',
        help_text='仅当上方选择"自定义地址"时使用；留空则退回到 jsDelivr',
    )
    bootstrap_custom_icons = models.CharField(
        '自定义 Bootstrap Icons CSS URL', max_length=500, blank=True, default='',
        help_text='仅当上方选择"自定义地址"时使用；留空则退回到 jsDelivr',
    )
    bootstrap_custom_js = models.CharField(
        '自定义 Bootstrap JS bundle URL', max_length=500, blank=True, default='',
        help_text='仅当上方选择"自定义地址"时使用；留空则退回到 jsDelivr',
    )

    # ---------- 静态文件 / 页面缓存 ----------
    static_cache_ttl_seconds = models.IntegerField(
        '静态文件 /static/ 缓存 TTL（秒）', default=86400,
        help_text='设置白噪声对 /static/ 文件的 Cache-Control max-age；默认 1 天',
    )

    class Meta:
        verbose_name = '站点配置'
        verbose_name_plural = '站点配置'

    @classmethod
    def monaco_base(cls):
        """Return currently active Monaco /min/vs base URL."""
        try:
            cfg = cls.objects.first()
        except Exception:
            cfg = None
        if cfg is None:
            version = '0.52.2'
            return f'https://cdn.jsdelivr.net/npm/monaco-editor@{version}/min/vs'
        version = (cfg.monaco_version or '').strip() or '0.52.2'
        tmpl = cls._SOURCE_TEMPLATES.get(cfg.monaco_source)
        if tmpl is None:
            custom = (cfg.monaco_custom_base or '').strip()
            if custom:
                return custom.rstrip('/')
            return f'https://cdn.jsdelivr.net/npm/monaco-editor@{version}/min/vs'
        return tmpl.format(version=version)

    @classmethod
    def bootstrap_urls(cls):
        """Return (css, icons, js_bundle) URLs for Bootstrap loading."""
        fallback = (
            'https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css',
            'https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.0/font/bootstrap-icons.css',
            'https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js',
        )
        try:
            cfg = cls.objects.first()
        except Exception:
            return fallback
        if cfg is None:
            return fallback
        css_v = (cfg.bootstrap_css_version or '').strip() or '5.3.0'
        icons_v = (cfg.bootstrap_icons_version or '').strip() or '1.10.0'
        templates = cls._BOOTSTRAP_SOURCE_TEMPLATES.get(cfg.bootstrap_source)
        if templates is None:
            custom_css = (cfg.bootstrap_custom_css or '').strip()
            custom_icons = (cfg.bootstrap_custom_icons or '').strip()
            custom_js = (cfg.bootstrap_custom_js or '').strip()
            if not (custom_css and custom_icons and custom_js):
                return fallback
            return custom_css, custom_icons, custom_js
        css_url, icons_url, js_url = templates
        return (
            css_url.format(css_version=css_v, icons_version=icons_v),
            icons_url.format(css_version=css_v, icons_version=icons_v),
            js_url.format(css_version=css_v, icons_version=icons_v),
        )

    @classmethod
    def static_cache_ttl(cls):
        """Return static file cache TTL in seconds; defaults to 1 day."""
        try:
            cfg = cls.objects.first()
        except Exception:
            return 86400
        if cfg is None:
            return 86400
        try:
            ttl = int(cfg.static_cache_ttl_seconds or 86400)
        except (TypeError, ValueError):
            return 86400
        if ttl <= 0:
            return 0
        return min(ttl, 31536000)
