from django.contrib import admin
from .models import Submission, SubmissionTestResult


class SubmissionTestResultInline(admin.TabularInline):
    model = SubmissionTestResult
    extra = 0
    readonly_fields = ['case_index', 'status', 'runtime', 'actual_output', 'expected_output']


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'problem', 'language', 'status', 'runtime', 'memory', 'created_at']
    list_filter = ['status', 'language', 'created_at']
    search_fields = ['user__username', 'problem__title']
    readonly_fields = ['created_at']
    inlines = [SubmissionTestResultInline]
