import re

from django.db import models
from django.contrib.auth import get_user_model
from django.core.cache import cache

User = get_user_model()

# Tags are free text. Historical rows mix comma lists ("dp,data structures")
# and space lists ("洛谷 P9755"). Prefer punctuation separators when present
# so multi-word algorithm tags stay intact.
_TAG_PUNCT_RE = re.compile(r'[,，、;；]+')
_TAG_SPACE_RE = re.compile(r'\s+')
_TAG_SPLIT_RE = re.compile(r'[\s,，、;；]+')  # kept for callers / search helpers


def split_stored_tags(raw: str) -> list[str]:
    """Split a stored ``tags`` string into individual labels."""
    text = (raw or '').strip()
    if not text:
        return []
    splitter = _TAG_PUNCT_RE if _TAG_PUNCT_RE.search(text) else _TAG_SPACE_RE
    return [tag for tag in splitter.split(text) if tag.strip()]


def public_tag_list(raw: str) -> list[str]:
    """Tags shown on the site: Chinese algorithm labels plus 中文来源."""
    from .tag_labels import CANONICAL_ZH_TAGS, has_cjk, is_provenance_tag, to_zh_algorithm_tag

    canonical = set(CANONICAL_ZH_TAGS)
    seen: list[str] = []
    for tag in split_stored_tags(raw):
        if is_provenance_tag(tag):
            if not has_cjk(tag):
                continue
            label = tag
        elif tag in canonical:
            label = tag
        else:
            label = to_zh_algorithm_tag(tag)
            if not label:
                continue
        if label not in seen:
            seen.append(label)
    return seen


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
        verbose_name = '题目'
        verbose_name_plural = '题目'

    def __str__(self):
        return f"P{self.id} - {self.title}"

    def _invalidate_caches(self):
        """Invalidate bounded cache keys without Redis-wide pattern deletes."""
        cache.delete_many([
            f'problem_pass_rate_{self.id}',
            'leaderboard_users',
            'home_recent_problems',
            'home_stats',
        ])
        # Version list caches instead of enumerating arbitrary query-string
        # keys. Increment is atomic on Redis and safely falls back elsewhere.
        try:
            cache.incr('problem_list_version')
        except (ValueError, TypeError):
            cache.set('problem_list_version', 1, None)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self._invalidate_caches()

    def delete(self, *args, **kwargs):
        self._invalidate_caches()
        super().delete(*args, **kwargs)

    @property
    def tag_list(self):
        """Tags normalized to a list.

        Historical data uses both separators: ``"洛谷,P1231"`` (comma) and
        ``"洛谷 P9755 CSP-S 2024"`` (spaces). Splitting on both keeps badge
        rendering and tag filtering consistent.
        """
        return public_tag_list(self.tags)

    @property
    def difficulty_slug(self):
        """Return a CSS-safe identifier for :attr:`difficulty`.

        The raw difficulty values (``普及-``, ``普及+``, ``提高+`` ...) contain
        ``+`` / ``-`` which are special in CSS selectors. Using a slugified
        class name (``pminus`` / ``pplus`` / ``tminus`` / ``tplus``) means the
        CSS rule always matches unambiguously.
        """
        return {
            '入门': 'intro',
            '普及-': 'pminus',
            '普及': 'puhui',
            '普及+': 'pplus',
            '提高-': 'tminus',
            '提高': 'tigao',
            '提高+': 'tplus',
            '省选': 'shengxuan',
            'NOI': 'noi',
        }.get(self.difficulty or '', 'puhui')

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
        verbose_name = '测试用例'
        verbose_name_plural = '测试用例'

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
        verbose_name = '官方题解'
        verbose_name_plural = '官方题解'

    def __str__(self):
        return f"{self.problem.title} - {self.title}"
    
    @property
    def like_count(self):
        return self.likes.count()
