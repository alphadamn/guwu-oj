from django.contrib import admin
from .models import Submission, SubmissionTestResult, JudgeMachine


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


@admin.register(JudgeMachine)
class JudgeMachineAdmin(admin.ModelAdmin):
    list_display = ['name', 'host', 'port', 'db', 'queue', 'enabled', 'weight']
    list_editable = ['host', 'port', 'db', 'queue', 'enabled', 'weight']
    list_filter = ['enabled']
    search_fields = ['name', 'host']
    actions = ['check_health']

    @admin.action(description='Check health of selected machines')
    def check_health(self, request, queryset):
        from .judge_load_balancer import load_balancer
        results = []
        for m in queryset:
            machine_dict = {
                'name': m.name, 'host': m.host, 'port': m.port,
                'db': m.db, 'queue': m.queue,
            }
            healthy = load_balancer.check_machine_health(machine_dict)
            status = '✓ healthy' if healthy else '✗ unreachable'
            results.append(f'{m.name}: {status}')
        self.message_user(request, '\n'.join(results))
