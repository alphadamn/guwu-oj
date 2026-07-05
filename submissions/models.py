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
        ('Assembly', 'Assembly'),
        ('Rust', 'Rust'),
        ('Golang', 'Golang'),
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
    language = models.CharField(max_length=10, choices=LANGUAGE_CHOICES)
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
        # Clear relevant caches when submission is saved
        cache.delete_pattern('views.decorators.cache.*')  # Clear all view caches
        cache.delete(f'problem_pass_rate_{self.problem.id}')  # Clear pass rate cache for this problem
        cache.delete('leaderboard_users')  # Clear leaderboard cache
        cache.delete_pattern('problem_list_query_*')  # Clear problem list query caches
        cache.delete('home_stats')  # Clear home stats cache


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
        # Ensure only one config record exists
        self.pk = 1
        super().save(*args, **kwargs)
        # Clear cache to force reload of settings
        cache.delete('judge_config')
