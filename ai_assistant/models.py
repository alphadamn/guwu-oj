from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .constants import (
    INTERVAL_MONTH,
    PLAN_CHOICES,
    PLAN_FREE,
)


class BillingConfig(models.Model):
    """Singleton holding the Stripe product/price IDs provisioned for the
    AI subscription plans. Populated lazily by ``ai_assistant.billing``.
    """

    plus_product_id = models.CharField('Plus Product ID', max_length=100, blank=True, default='')
    pro_product_id = models.CharField('Pro Product ID', max_length=100, blank=True, default='')
    plus_monthly_price_id = models.CharField('Plus 月付 Price ID', max_length=100, blank=True, default='')
    plus_yearly_price_id = models.CharField('Plus 年付 Price ID', max_length=100, blank=True, default='')
    pro_monthly_price_id = models.CharField('Pro 月付 Price ID', max_length=100, blank=True, default='')
    pro_yearly_price_id = models.CharField('Pro 年付 Price ID', max_length=100, blank=True, default='')
    updated_at = models.DateTimeField('最后更新时间', auto_now=True)

    class Meta:
        verbose_name = '订阅计费配置'
        verbose_name_plural = '订阅计费配置'

    def save(self, *args, **kwargs):
        if self.pk is None:
            self.pk = 1
        return super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls):
        config, _ = cls.objects.get_or_create(pk=1)
        return config

    def price_id(self, plan: str, interval: str) -> str:
        field = {
            ('plus', 'month'): 'plus_monthly_price_id',
            ('plus', 'year'): 'plus_yearly_price_id',
            ('pro', 'month'): 'pro_monthly_price_id',
            ('pro', 'year'): 'pro_yearly_price_id',
        }.get((plan, interval), '')
        return getattr(self, field, '') if field else ''

    def set_price_id(self, plan: str, interval: str, value: str) -> None:
        field = {
            ('plus', 'month'): 'plus_monthly_price_id',
            ('plus', 'year'): 'plus_yearly_price_id',
            ('pro', 'month'): 'pro_monthly_price_id',
            ('pro', 'year'): 'pro_yearly_price_id',
        }.get((plan, interval))
        if field:
            setattr(self, field, value)

    def product_id(self, plan: str) -> str:
        return self.plus_product_id if plan == 'plus' else self.pro_product_id

    def set_product_id(self, plan: str, value: str) -> None:
        if plan == 'plus':
            self.plus_product_id = value
        elif plan == 'pro':
            self.pro_product_id = value


class Subscription(models.Model):
    """A user's AI subscription. A missing (or expired) row means free tier."""

    class Status(models.TextChoices):
        ACTIVE = 'active', '生效中'
        CANCELED = 'canceled', '已取消'

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='ai_subscription',
    )
    plan = models.CharField('套餐', max_length=16, choices=PLAN_CHOICES, default=PLAN_FREE)
    status = models.CharField('状态', max_length=16, choices=Status.choices, default=Status.ACTIVE)
    interval = models.CharField('计费周期', max_length=8, blank=True, default='')
    stripe_customer_id = models.CharField('Stripe Customer ID', max_length=100, blank=True, default='', db_index=True)
    stripe_subscription_id = models.CharField('Stripe Subscription ID', max_length=100, blank=True, default='', db_index=True)
    current_period_start = models.DateTimeField('当前周期开始', null=True, blank=True)
    current_period_end = models.DateTimeField('当前周期结束', null=True, blank=True)
    cancel_at_period_end = models.BooleanField('周期结束后取消', default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'AI 订阅'
        verbose_name_plural = 'AI 订阅'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user} · {self.get_plan_display()} ({self.status})'

    @property
    def is_paid_active(self) -> bool:
        """True while a paid plan is in force for the current instant."""
        if self.plan == PLAN_FREE or self.status != self.Status.ACTIVE:
            return False
        if self.current_period_end is None:
            return True
        return self.current_period_end > timezone.now()


class AISession(models.Model):
    """One short AI interaction on a problem: an initial answer plus at most
    one regeneration. Marking it satisfied locks further generations."""

    class Status(models.TextChoices):
        ACTIVE = 'active', '进行中'
        SATISFIED = 'satisfied', '已采纳'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ai_sessions',
    )
    problem = models.ForeignKey(
        'problems.Problem', on_delete=models.CASCADE, related_name='ai_sessions',
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'AI 解题会话'
        verbose_name_plural = 'AI 解题会话'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'problem', '-created_at']),
        ]

    def __str__(self):
        return f'AI session {self.id} ({self.user} / P{self.problem_id})'

    @property
    def successful_generation_count(self) -> int:
        return self.generations.filter(success=True).count()


class AIGeneration(models.Model):
    """A single AI answer within a session. Successful rows are the durable
    source of truth for quota counting."""

    session = models.ForeignKey(
        AISession, on_delete=models.CASCADE, related_name='generations',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ai_generations',
    )
    problem = models.ForeignKey(
        'problems.Problem', on_delete=models.CASCADE, related_name='ai_generations',
    )
    round_no = models.PositiveSmallIntegerField('轮次', default=1)
    prompt = models.TextField('发送给模型的提示词')
    reasoning = models.TextField('模型思考过程', blank=True, default='')
    answer = models.TextField('模型回答', blank=True, default='')
    prompt_tokens = models.PositiveIntegerField(default=0)
    completion_tokens = models.PositiveIntegerField(default=0)
    success = models.BooleanField('是否成功', default=True)
    error_message = models.CharField('错误信息', max_length=300, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'AI 生成记录'
        verbose_name_plural = 'AI 生成记录'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'success', '-created_at']),
        ]

    def __str__(self):
        return f'AI gen {self.id} round {self.round_no} ({self.user})'


class AIToolCall(models.Model):
    """One invocation of a model-side tool during a generation.

    The only tool today is ``submit_to_judge``: the model submits a complete
    reference program under the dedicated AI service account so the real
    judge can verify its approach. At most
    ``constants.MAX_JUDGE_TOOL_CALLS`` rows are created per generation.
    """

    class Status(models.TextChoices):
        PENDING = 'Pending', '评测中'
        ACCEPTED = 'Accepted', 'Accepted'
        WRONG_ANSWER = 'Wrong Answer', 'Wrong Answer'
        TLE = 'Time Limit Exceeded', 'Time Limit Exceeded'
        MLE = 'Memory Limit Exceeded', 'Memory Limit Exceeded'
        RUNTIME_ERROR = 'Runtime Error', 'Runtime Error'
        COMPILE_ERROR = 'Compile Error', 'Compile Error'
        SYSTEM_ERROR = 'System Error', 'System Error'
        TIMEOUT = 'Timeout', '等待判题超时'
        INVALID = 'Invalid', '参数无效'
        ERROR = 'Error', '调用失败'

    generation = models.ForeignKey(
        AIGeneration, on_delete=models.CASCADE, related_name='tool_calls',
    )
    seq = models.PositiveSmallIntegerField('第几次工具调用')
    tool_name = models.CharField('工具名称', max_length=64, default='submit_to_judge')
    language = models.CharField('语言', max_length=32, blank=True, default='')
    code = models.TextField('提交的代码', blank=True, default='')
    submission = models.ForeignKey(
        'submissions.Submission', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='ai_tool_calls',
    )
    status = models.CharField(
        '评测结果', max_length=30, choices=Status.choices, default=Status.PENDING,
    )
    passed_cases = models.PositiveIntegerField('通过测试点数', default=0)
    total_cases = models.PositiveIntegerField('总测试点数', default=0)
    runtime_ms = models.PositiveIntegerField('最大耗时(ms)', null=True, blank=True)
    memory_kb = models.PositiveIntegerField('最大内存(KB)', null=True, blank=True)
    result_json = models.TextField('返回给模型的结果', blank=True, default='')
    error_message = models.CharField('错误信息', max_length=300, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'AI 工具调用'
        verbose_name_plural = 'AI 工具调用'
        ordering = ['created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['generation', 'seq'], name='uniq_ai_tool_seq_per_generation',
            ),
        ]

    def __str__(self):
        return f'AI tool call gen={self.generation_id} #{self.seq} ({self.status})'

    @property
    def is_accepted(self) -> bool:
        return self.status == self.Status.ACCEPTED

    # --- UI helpers (used by ask.html / ask.js parity) ---------------------
    _VERDICT_LABELS = {
        'Pending': '评测中',
        'Accepted': 'AC',
        'Wrong Answer': '答案错误',
        'Time Limit Exceeded': '超时',
        'Memory Limit Exceeded': '超内存',
        'Runtime Error': '运行错误',
        'Compile Error': '编译错误',
        'System Error': '评测系统错误',
        'Timeout': '判题超时',
        'Invalid': '参数无效',
        'Error': '调用失败',
    }

    @property
    def verdict_label(self) -> str:
        return self._VERDICT_LABELS.get(self.status, self.status)

    @property
    def verdict_class(self) -> str:
        if self.status == self.Status.ACCEPTED:
            return 'is-accepted'
        if self.status in (self.Status.PENDING, self.Status.TIMEOUT):
            return 'is-pending'
        return 'is-failed'
