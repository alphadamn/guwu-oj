from django.contrib import admin
from .models import Contest, ContestProblem, ContestTestCase


class ContestTestCaseInline(admin.TabularInline):
    model = ContestTestCase
    extra = 3


class ContestProblemInline(admin.TabularInline):
    model = ContestProblem
    extra = 0
    fields = ['order', 'title', 'difficulty', 'time_limit', 'memory_limit', 'published_problem']
    readonly_fields = ['published_problem']


@admin.register(Contest)
class ContestAdmin(admin.ModelAdmin):
    list_display = ['name', 'creator', 'start_at', 'end_at', 'max_submissions_per_problem', 'published_at']
    list_filter = ['start_at', 'end_at', 'published_at']
    search_fields = ['name', 'description', 'creator__username']
    inlines = [ContestProblemInline]


@admin.register(ContestProblem)
class ContestProblemAdmin(admin.ModelAdmin):
    list_display = ['title', 'contest', 'order', 'difficulty', 'created_by', 'published_problem']
    list_filter = ['contest', 'difficulty']
    search_fields = ['title', 'description', 'tags']
    readonly_fields = ['published_problem', 'created_at', 'updated_at']
    inlines = [ContestTestCaseInline]


@admin.register(ContestTestCase)
class ContestTestCaseAdmin(admin.ModelAdmin):
    list_display = ['id', 'contest_problem', 'order', 'is_sample']
    list_filter = ['contest_problem__contest']
