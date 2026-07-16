from django.contrib import admin
from .models import Contest, ContestProblem


class ContestProblemInline(admin.TabularInline):
    model = ContestProblem
    extra = 0


@admin.register(Contest)
class ContestAdmin(admin.ModelAdmin):
    list_display = ['name', 'creator', 'start_at', 'end_at', 'max_submissions_per_problem', 'published_at']
    list_filter = ['start_at', 'end_at', 'published_at']
    search_fields = ['name', 'description', 'creator__username']
    inlines = [ContestProblemInline]


@admin.register(ContestProblem)
class ContestProblemAdmin(admin.ModelAdmin):
    list_display = ['contest', 'problem', 'order']
    list_filter = ['contest']
