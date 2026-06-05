from django.db import models
from django.contrib.auth import get_user_model
from django.core.cache import cache

User = get_user_model()


class Problem(models.Model):
    DIFFICULTY_CHOICES = [
        ('入门', '入门'),
        ('普及-', '普及-'),
        ('普及', '普及'),
        ('普及+', '普及+'),
        ('提高-', '提高-'),
        ('提高', '提高'),
        ('提高+', '提高+'),
        ('省选', '省选'),
        ('NOI', 'NOI'),
    ]
    
    title = models.CharField(max_length=200)
    description = models.TextField()
    input_format = models.TextField()
    output_format = models.TextField()
    sample_input = models.TextField(blank=True)
    sample_output = models.TextField(blank=True)
    hint = models.TextField(blank=True)
    difficulty = models.CharField(max_length=10, choices=DIFFICULTY_CHOICES, default='普及')
    time_limit = models.IntegerField(
        default=1000,
        help_text='时间限制（毫秒）。评测程序会按毫秒换算为秒。',
    )
    memory_limit = models.IntegerField(default=256, help_text='内存限制（MB）')
    tags = models.CharField(max_length=200, blank=True)
    luogu_pid = models.CharField(
        max_length=16, blank=True, null=True, unique=True,
        help_text='洛谷题号，如 P1000',
    )
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_problems')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_public = models.BooleanField(default=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"P{self.id} - {self.title}"
    
    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Clear relevant caches when problem is saved
        cache.delete_pattern('views.decorators.cache.*')  # Clear all view caches
        cache.delete(f'problem_pass_rate_{self.id}')  # Clear pass rate cache
        cache.delete('leaderboard_users')  # Clear leaderboard cache
        cache.delete_pattern('problem_list_query_*')  # Clear problem list query caches
        cache.delete('home_recent_problems')  # Clear home recent problems cache
        cache.delete('home_stats')  # Clear home stats cache

    def delete(self, *args, **kwargs):
        # Clear relevant caches before deletion
        cache.delete_pattern('views.decorators.cache.*')
        cache.delete(f'problem_pass_rate_{self.id}')
        cache.delete('leaderboard_users')
        cache.delete_pattern('problem_list_query_*')
        cache.delete('home_recent_problems')
        cache.delete('home_stats')
        super().delete(*args, **kwargs)

    @property
    def pass_rate(self):
        """Calculate pass rate as percentage of accepted submissions."""
        from django.core.cache import cache
        cache_key = f'problem_pass_rate_{self.id}'
        cached_rate = cache.get(cache_key)
        if cached_rate is not None:
            return cached_rate

        total = self.submissions.count()
        if total == 0:
            rate = 0.0
        else:
            accepted = self.submissions.filter(status='Accepted').count()
            rate = round(accepted * 100.0 / total, 1)

        cache.set(cache_key, rate, 60 * 5)  # Cache for 5 minutes
        return rate


class TestCase(models.Model):
    problem = models.ForeignKey(Problem, on_delete=models.CASCADE, related_name='test_cases')
    input_data = models.TextField(blank=True)
    expected_output = models.TextField()
    order = models.PositiveIntegerField(default=0)
    is_sample = models.BooleanField(default=False)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return f"TestCase {self.id} for P{self.problem_id}"


class Solution(models.Model):
    problem = models.ForeignKey(Problem, on_delete=models.CASCADE, related_name='solutions')
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name='solutions')
    title = models.CharField(max_length=200)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_approved = models.BooleanField(default=False)  # 需要管理员审核
    likes = models.ManyToManyField(User, related_name='liked_solutions', blank=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.problem.title} - {self.title}"
    
    @property
    def like_count(self):
        return self.likes.count()
