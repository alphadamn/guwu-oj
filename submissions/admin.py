from django import forms
from django.contrib import admin
from .models import Submission, SubmissionTestResult, JudgeMachine, JudgeConfig


class JudgeMachineAdminForm(forms.ModelForm):
    redis_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text='Leave blank to keep the current password. It is encrypted at rest.',
    )

    class Meta:
        model = JudgeMachine
        fields = '__all__'
        exclude = ['redis_password_encrypted']

    def clean(self):
        cleaned = super().clean()
        cert = cleaned.get('client_cert_path', '').strip()
        key = cleaned.get('client_key_path', '').strip()
        if bool(cert) != bool(key):
            raise forms.ValidationError(
                'Client certificate and client key must be configured together.'
            )
        if cleaned.get('transport_configured') and cleaned.get('tls_enabled'):
            if not cleaned.get('ca_cert_path', '').strip():
                self.add_error('ca_cert_path', 'A CA certificate path is required for TLS.')
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        password = self.cleaned_data.get('redis_password')
        if password:
            instance.set_redis_password(password)
        if commit:
            instance.save()
            self.save_m2m()
        return instance


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
    form = JudgeMachineAdminForm
    list_display = [
        'name', 'host', 'port', 'db', 'queue', 'enabled', 'weight',
        'transport_configured', 'tls_enabled',
    ]
    list_editable = ['host', 'port', 'db', 'queue', 'enabled', 'weight']
    list_filter = ['enabled', 'transport_configured', 'tls_enabled']
    search_fields = ['name', 'host']
    readonly_fields = ['redis_password_encrypted']
    fieldsets = (
        (None, {'fields': ('name', 'host', 'port', 'db', 'queue', 'enabled', 'weight')}),
        ('Redis transport security', {
            'fields': (
                'transport_configured', 'tls_enabled', 'ca_cert_path',
                'client_cert_path', 'client_key_path', 'redis_password',
            ),
            'description': (
                'Paths refer to files on the judge host. Do not paste PEM content. '
                'Enable transport configuration to override environment defaults.'
            ),
        }),
    )
    actions = ['check_health', 'check_judging_system']

    @admin.action(description='Check health of selected machines')
    def check_health(self, request, queryset):
        from .judge_load_balancer import load_balancer
        results = []
        for m in queryset:
            machine_dict = load_balancer.effective_machine(m.name) or {
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
