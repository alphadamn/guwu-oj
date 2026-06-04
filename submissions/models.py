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
    
    def __str__(self):
        return f"Submission {self.id} - {self.user.username} - {self.problem.title}"
    
    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Clear relevant caches when submission is saved
        cache.delete_pattern('views.decorators.cache.*')  # Clear all view caches


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
