from django.db import models, transaction
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.text import slugify

from problems.models import Problem

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
            for item in contest.problems.select_related('problem'):
                problem = item.problem
                tags = [value for value in (problem.tags or '').split() if value]
                if tag not in tags:
                    tags.append(tag)
                problem.tags = ' '.join(tags)
                problem.is_public = True
                problem.save(update_fields=['tags', 'is_public', 'updated_at'])
            contest.published_at = timezone.now()
            contest.save(update_fields=['published_at', 'updated_at'])
        return True


class ContestProblem(models.Model):
    contest = models.ForeignKey(Contest, on_delete=models.CASCADE, related_name='problems')
    problem = models.OneToOneField(Problem, on_delete=models.CASCADE, related_name='contest_problem')
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']
        constraints = [models.UniqueConstraint(fields=['contest', 'problem'], name='unique_contest_problem')]

    def __str__(self):
        return f'{self.contest.name}: {self.problem.title}'
