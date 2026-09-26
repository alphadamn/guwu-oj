from django.conf import settings
from django.db import models


class Ticket(models.Model):
    """用户提交的工单（问题反馈 / 功能建议等），提交后邮件通知所有管理员。"""

    class Category(models.TextChoices):
        BUG = 'bug', '问题反馈'
        FEATURE = 'feature', '功能建议'
        ACCOUNT = 'account', '账号问题'
        OTHER = 'other', '其他'

    class Status(models.TextChoices):
        PENDING = 'pending', '待处理'
        PROCESSING = 'processing', '处理中'
        RESOLVED = 'resolved', '已解决'
        CLOSED = 'closed', '已关闭'

    subject = models.CharField('主题', max_length=100)
    category = models.CharField(
        '分类', max_length=20, choices=Category.choices, default=Category.OTHER,
    )
    body = models.TextField('内容', max_length=5000)
    submitter = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name='提交人',
        on_delete=models.PROTECT, related_name='tickets',
    )
    contact_email = models.EmailField(
        '联系邮箱', blank=True, default='',
        help_text='留空时自动使用账号邮箱',
    )
    status = models.CharField(
        '状态', max_length=20, choices=Status.choices,
        default=Status.PENDING, db_index=True,
    )
    admin_note = models.TextField('处理备注', blank=True, default='')
    email_sent = models.BooleanField('通知邮件已发送', default=False)
    email_error = models.TextField('通知邮件错误', blank=True, default='')
    created_at = models.DateTimeField('创建时间', auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '工单'
        verbose_name_plural = '工单'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at']),
        ]

    def __str__(self):
        return f'#{self.pk} {self.subject}'
