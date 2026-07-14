from django.db import models
from django.contrib.auth import get_user_model
from django.core.cache import cache
from problems.models import Problem

User = get_user_model()


class Submission(models.Model):
    LANGUAGE_CHOICES = [
        ('C', 'C'),
        ('C++', 'C++'),
        ('Python', 'Python'),
        ('Java', 'Java'),
        ('JavaScript', 'JavaScript'),
        ('Golang', 'Golang'),
        ('Rust', 'Rust'),
        ('Ruby', 'Ruby'),
        ('Kotlin', 'Kotlin'),
        ('Assembly', 'Assembly'),
    ]
    
    STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Accepted', 'Accepted'),
        ('Wrong Answer', 'Wrong Answer'),
        ('Time Limit Exceeded', 'Time Limit Exceeded'),
        ('Memory Limit Exceeded', 'Memory Limit Exceeded'),
        ('Runtime Error', 'Runtime Error'),
        ('Compile Error', 'Compile Error'),
    ]
    
    problem = models.ForeignKey(Problem, on_delete=models.CASCADE, related_name='submissions')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='submissions')
    code = models.TextField()
    language = models.CharField(max_length=32, choices=LANGUAGE_CHOICES)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='Pending')
    runtime = models.IntegerField(blank=True, null=True)  # in milliseconds
    memory = models.IntegerField(blank=True, null=True)  # in KB
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = '提交记录'
        verbose_name_plural = '提交记录'

    def __str__(self):
        return f"Submission {self.id} - {self.user.username} - {self.problem.title}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Clear only caches directly related to this problem.
        # Avoids Redis KEYS (O(N) blocking) used by delete_pattern;
        # also avoids invalidating unrelated keys on every submission.
        problem_id = self.problem_id
        cache.delete(f'problem_pass_rate_{problem_id}')
        # Use SCAN-based iteration (non-blocking) for pattern matches.
        try:
            from django_redis import get_redis_connection
            redis_conn = get_redis_connection('default')
            for key in redis_conn.scan_iter(match=f'problem_list_query_{problem_id}_*', count=200):
                redis_conn.delete(key)
        except Exception:
            pass  # Non-redis backend or unavailable — fail silently on cache cleanup


class SubmissionTestResult(models.Model):
    CASE_STATUS_CHOICES = [
        ('Accepted', 'Accepted'),
        ('Wrong Answer', 'Wrong Answer'),
        ('Time Limit Exceeded', 'Time Limit Exceeded'),
        ('Memory Limit Exceeded', 'Memory Limit Exceeded'),
        ('Runtime Error', 'Runtime Error'),
        ('Skipped', 'Skipped'),
    ]

    submission = models.ForeignKey(
        Submission, on_delete=models.CASCADE, related_name='test_results'
    )
    test_case = models.ForeignKey(
        'problems.TestCase', on_delete=models.SET_NULL, null=True, blank=True
    )
    case_index = models.PositiveIntegerField()
    status = models.CharField(max_length=30, choices=CASE_STATUS_CHOICES)
    runtime = models.IntegerField(blank=True, null=True)
    actual_output = models.TextField(blank=True)
    expected_output = models.TextField(blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ['case_index']
        unique_together = [['submission', 'case_index']]

    def __str__(self):
        return f"Submission {self.submission_id} case #{self.case_index}: {self.status}"


class JudgeMachine(models.Model):
    """Judge machine configuration for distributed judging."""
    name = models.CharField(max_length=64, unique=True)
    host = models.CharField(max_length=255, default='localhost')
    port = models.IntegerField(default=6379)
    db = models.IntegerField(default=0)
    queue = models.CharField(max_length=64)
    enabled = models.BooleanField(default=True)
    weight = models.IntegerField(default=1, help_text='Higher weight = more tasks')

    class Meta:
        ordering = ['name']
        verbose_name = '评测机'
        verbose_name_plural = '评测机'

    def __str__(self):
        status = '✓' if self.enabled else '✗'
        return f'{status} {self.name} ({self.host}:{self.port}/{self.db})'


class JudgeConfig(models.Model):
    """Global judge configuration settings."""
    subprocess_timeout_sec = models.IntegerField(
        default=5,
        help_text='Global subprocess timeout in seconds (safety net for all executions)'
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '评测配置'
        verbose_name_plural = '评测配置'

    def __str__(self):
        return f'Judge Config (timeout: {self.subprocess_timeout_sec}s)'

    def save(self, *args, **kwargs):
        # Thread-safe singleton: use a transaction + get_or_create on a
        # fixed pk. The naive self.pk = 1 before save is a race condition.
        from django.db import transaction
        with transaction.atomic():
            if not self.pk:
                existing, created = JudgeConfig.objects.get_or_create(pk=1)
                if not created:
                    existing.subprocess_timeout_sec = self.subprocess_timeout_sec
                    existing.save()
                    self.pk = existing.pk
                    return
            else:
                # Existing record — enforce pk=1 to prevent multiple rows
                if self.pk != 1:
                    self.pk = 1
                super().save(*args, **kwargs)
        # Clear cache to force reload of settings
        cache.delete('judge_config')
