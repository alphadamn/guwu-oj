from django.contrib import admin, messages
from django.db import transaction
from django.utils import timezone

from .models import Contest, ContestEnrollment, ContestProblem, ContestTestCase


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
    list_display = ['name', 'creator', 'start_at', 'end_at', 'max_submissions_per_problem', 'entry_points_cost', 'published_at']
    list_filter = ['start_at', 'end_at', 'published_at']
    search_fields = ['name', 'description', 'creator__username']
    inlines = [ContestProblemInline]
    actions = ['end_selected_contests']

    @admin.action(description='结束并发布/修复选中的竞赛')
    def end_selected_contests(self, request, queryset):
        ended = 0
        repaired = 0
        already_complete = 0
        for contest_id in queryset.values_list('id', flat=True):
            with transaction.atomic():
                contest = Contest.objects.select_for_update().get(pk=contest_id)
                was_finished = contest.is_finished
                if not was_finished:
                    contest.end_at = timezone.now()
                    contest.save(update_fields=['end_at', 'updated_at'])
                if contest.publish_finished_problems():
                    if was_finished:
                        repaired += 1
                    else:
                        ended += 1
                else:
                    already_complete += 1
        self.message_user(
            request,
            f'已结束并发布 {ended} 个竞赛；修复并发布 {repaired} 个竞赛；{already_complete} 个竞赛此前已完整发布。',
            level=messages.SUCCESS,
        )


@admin.register(ContestEnrollment)
class ContestEnrollmentAdmin(admin.ModelAdmin):
    list_display = ['contest', 'user', 'points_cost', 'enrolled_at']
    list_filter = ['contest']
    search_fields = ['contest__name', 'user__username']
    readonly_fields = ['contest', 'user', 'points_cost', 'enrolled_at']

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

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
