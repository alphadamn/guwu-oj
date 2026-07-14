from django.contrib import admin
from .models import Submission, SubmissionTestResult, JudgeMachine, JudgeConfig


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
    actions = ['check_health', 'check_judging_system']

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

    @admin.action(description='Check whole judging system health (submits test code)')
    def check_judging_system(self, request, queryset):
        from devlog.views import _check_judging_system
        from devlog.models import ServiceComponent

        ac_count = _check_judging_system()

        # Map AC count to status message
        status_map = {
            3: '正常运作 (3/3 test cases AC)',
            2: '性能下降 (2/3 test cases AC)',
            1: '部分中断 (1/3 test cases AC)',
            0: '重大中断 (0/3 test cases AC)',
        }
        message = status_map.get(ac_count, f'未知状态 ({ac_count} AC)')

        # Update the judge component in devlog
        judge_comp = ServiceComponent.objects.filter(name='评测系统').first()
        if judge_comp:
            if ac_count == 3:
                judge_comp.status = ServiceComponent.STATUS_OPERATIONAL
            elif ac_count == 2:
                judge_comp.status = ServiceComponent.STATUS_DEGRADED
            elif ac_count == 1:
                judge_comp.status = ServiceComponent.STATUS_PARTIAL
            else:
                judge_comp.status = ServiceComponent.STATUS_MAJOR
            judge_comp.save(update_fields=['status', 'updated_at'])

        self.message_user(request, f'Judging system health check: {message}')


@admin.register(JudgeConfig)
class JudgeConfigAdmin(admin.ModelAdmin):
    list_display = ['subprocess_timeout_sec', 'updated_at']
    readonly_fields = ['updated_at']

    def has_add_permission(self, request):
        # Only allow one config record
        return not JudgeConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        # Prevent deletion
        return False
