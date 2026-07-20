from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models


class PointConfig(models.Model):
    """Singleton settings for the site's points economy."""

    inviter_registration_points = models.PositiveIntegerField(
        '邀请人注册奖励积分', default=0,
        help_text='通过邀请链接成功注册后，邀请人获得的积分。0 表示关闭奖励。',
    )
    invitee_registration_points = models.PositiveIntegerField(
        '受邀人注册奖励积分', default=0,
        help_text='通过邀请链接成功注册后，新用户获得的积分。0 表示关闭奖励。',
    )
    accepted_testcase_points = models.DecimalField(
        'AC 测试点奖励积分', max_digits=12, decimal_places=2, default=0,
        validators=[MinValueValidator(0)],
        help_text='普通题目中，用户首次通过某个非样例测试点时获得的积分，最多保留两位小数。',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '积分配置'
        verbose_name_plural = '积分配置'

    def save(self, *args, **kwargs):
        if self.pk is None:
            self.pk = 1
        return super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls):
        config, _ = cls.objects.get_or_create(pk=1)
        return config


class PointLedgerEntry(models.Model):
    """Immutable, uniquely keyed credits and debits applied to a user."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='point_ledger_entries')
    amount = models.DecimalField('积分变动', max_digits=12, decimal_places=2)
    balance_after = models.DecimalField('变动后余额', max_digits=12, decimal_places=2)
    event_type = models.CharField('事件类型', max_length=50)
    event_key = models.CharField('事件键', max_length=200)
    description = models.CharField('说明', max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        verbose_name = '积分流水'
        verbose_name_plural = '积分流水'
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'event_type', 'event_key'],
                name='unique_user_point_event',
            ),
        ]

    def __str__(self):
        return f'{self.user} {self.amount:+d} ({self.event_type})'
