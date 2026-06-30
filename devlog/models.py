from django.conf import settings
from django.db import models
from django.utils import timezone


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

    name = models.CharField('名称', max_length=100)
    name_en = models.CharField('英文名', max_length=100, blank=True)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default=STATUS_OPERATIONAL)
    description = models.CharField('说明', max_length=255, blank=True)
    uptime = models.FloatField('90 天可用率(%)', default=100.0)
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
