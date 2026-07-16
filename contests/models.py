from django.contrib.auth import get_user_model
from django.db import models, transaction
from django.utils import timezone
from django.utils.text import slugify

from problems.models import Problem, TestCase

User = get_user_model()


class Contest(models.Model):
    name = models.CharField(max_length=200, unique=True)
    description = models.TextField(blank=True)
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    max_submissions_per_problem = models.PositiveIntegerField(default=3)
    creator = models.ForeignKey(User, on_delete=models.PROTECT, related_name='created_contests')
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-start_at', '-id']
        permissions = [('manage_contest', 'Can manage contest content')]

    def __str__(self):
        return self.name

    @property
    def is_upcoming(self):
        return timezone.now() < self.start_at

    @property
    def is_live(self):
        now = timezone.now()
        return self.start_at <= now < self.end_at

    @property
    def is_finished(self):
        return timezone.now() >= self.end_at

    def publish_finished_problems(self):
        if not self.is_finished:
            return False
        with transaction.atomic():
            contest = Contest.objects.select_for_update().get(pk=self.pk)
            if contest.published_at:
                return False
            tag = f'contest:{slugify(contest.name)[:190] or contest.pk}'
            for item in contest.problems.prefetch_related('test_cases'):
                if item.published_problem_id:
                    continue
                tags = [value for value in (item.tags or '').split() if value]
                if tag not in tags:
                    tags.append(tag)
                published = Problem.objects.create(
                    title=item.title,
                    description=item.description,
                    input_format=item.input_format,
                    output_format=item.output_format,
                    sample_input=item.sample_input,
                    sample_output=item.sample_output,
                    hint=item.hint,
                    difficulty=item.difficulty,
                    time_limit=item.time_limit,
                    memory_limit=item.memory_limit,
                    tags=' '.join(tags),
                    created_by=item.created_by,
                    is_public=True,
                )
                TestCase.objects.bulk_create([
                    TestCase(
                        problem=published,
                        input_data=test_case.input_data,
                        expected_output=test_case.expected_output,
                        order=test_case.order,
                        is_sample=test_case.is_sample,
                    )
                    for test_case in item.test_cases.all()
                ])
                item.published_problem = published
                item.save(update_fields=['published_problem', 'updated_at'])
            contest.published_at = timezone.now()
            contest.save(update_fields=['published_at', 'updated_at'])
        return True


class ContestProblem(models.Model):
    DIFFICULTY_CHOICES = Problem.DIFFICULTY_CHOICES

    contest = models.ForeignKey(Contest, on_delete=models.CASCADE, related_name='problems')
    title = models.CharField(max_length=200)
    description = models.TextField()
    input_format = models.TextField()
    output_format = models.TextField()
    sample_input = models.TextField(blank=True)
    sample_output = models.TextField(blank=True)
    hint = models.TextField(blank=True)
    difficulty = models.CharField(max_length=10, choices=DIFFICULTY_CHOICES, default='普及')
    time_limit = models.IntegerField(default=1000)
    memory_limit = models.IntegerField(default=256)
    tags = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='created_contest_problems')
    order = models.PositiveIntegerField(default=0)
    published_problem = models.OneToOneField(
        Problem, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='published_from_contest_problem',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['order', 'id']
        constraints = [models.UniqueConstraint(fields=['contest', 'order'], name='unique_contest_problem_order')]

    def __str__(self):
        return f'{self.contest.name}: {self.title}'

    @property
    def difficulty_slug(self):
        return Problem.difficulty_slug.fget(self)


class ContestTestCase(models.Model):
    contest_problem = models.ForeignKey(ContestProblem, on_delete=models.CASCADE, related_name='test_cases')
    input_data = models.TextField(blank=True)
    expected_output = models.TextField()
    order = models.PositiveIntegerField(default=0)
    is_sample = models.BooleanField(default=False)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return f'Contest test case {self.id} for {self.contest_problem_id}'
