from django.db import models
from django.contrib.auth import get_user_model
from base64 import urlsafe_b64encode
from hashlib import sha256

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
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
        ('System Error', 'System Error'),
    ]
    
    problem = models.ForeignKey(Problem, null=True, blank=True, on_delete=models.CASCADE, related_name='submissions')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='submissions')
    contest_problem = models.ForeignKey(
        'contests.ContestProblem', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='submissions', db_index=True,
    )
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

    @property
    def effective_problem(self):
        """Return the judging target without dereferencing a nullable relation.

        Contest submissions intentionally have no normal ``Problem`` row. The
        explicit ID checks keep worker/admin code from evaluating
        ``self.problem`` for those rows.
        """
        if self.contest_problem_id:
            return self.contest_problem
        if self.problem_id:
            return self.problem
        return None

    def __str__(self):
        problem = self.effective_problem
        return f"Submission {self.id} - {self.user.username} - {problem.title if problem else 'unknown'}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.problem_id:
            return
        cache.delete(f'problem_pass_rate_{self.problem_id}')
        try:
            from django_redis import get_redis_connection
            redis_conn = get_redis_connection('default')
            for key in redis_conn.scan_iter(match=f'problem_list_query_{self.problem_id}_*', count=200):
                redis_conn.delete(key)
        except Exception:
            pass


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
    contest_test_case = models.ForeignKey(
        'contests.ContestTestCase', on_delete=models.SET_NULL, null=True, blank=True
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
    transport_configured = models.BooleanField(
        default=False,
        help_text='Use the TLS/password settings below instead of JUDGE_MACHINES_JSON defaults.',
    )
    tls_enabled = models.BooleanField(default=False)
    ca_cert_path = models.CharField(max_length=500, blank=True)
    client_cert_path = models.CharField(max_length=500, blank=True)
    client_key_path = models.CharField(max_length=500, blank=True)
    redis_password_encrypted = models.TextField(blank=True, editable=False)

    def _password_cipher(self):
        key = urlsafe_b64encode(sha256(settings.SECRET_KEY.encode()).digest())
        return Fernet(key)

    def set_redis_password(self, password):
        self.redis_password_encrypted = (
            self._password_cipher().encrypt(password.encode()).decode() if password else ''
        )

    def get_redis_password(self):
        if not self.redis_password_encrypted:
            return ''
        try:
            return self._password_cipher().decrypt(
                self.redis_password_encrypted.encode()
            ).decode()
        except (InvalidToken, UnicodeDecodeError):
            return ''

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
        """Persist the sole configuration row under a database row lock."""
        from django.db import IntegrityError, transaction

        timeout = self.subprocess_timeout_sec
        with transaction.atomic():
            try:
                config = JudgeConfig.objects.select_for_update().get(pk=1)
            except JudgeConfig.DoesNotExist:
                try:
                    # A savepoint keeps the surrounding transaction usable if
                    # another worker inserts the singleton concurrently.
                    with transaction.atomic():
                        config = JudgeConfig(pk=1, subprocess_timeout_sec=timeout)
                        models.Model.save(config, force_insert=True)
                except IntegrityError:
                    config = JudgeConfig.objects.select_for_update().get(pk=1)
                    config.subprocess_timeout_sec = timeout
                    models.Model.save(config, update_fields=['subprocess_timeout_sec', 'updated_at'])
            else:
                config.subprocess_timeout_sec = timeout
                models.Model.save(config, update_fields=['subprocess_timeout_sec', 'updated_at'])
            self.pk = config.pk
            self.updated_at = config.updated_at
        cache.delete('judge_config')
