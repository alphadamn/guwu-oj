from django.contrib import admin

from .models import AIGeneration, AISession, AIToolCall, BillingConfig, Subscription


@admin.register(BillingConfig)
class BillingConfigAdmin(admin.ModelAdmin):
    list_display = ('pk', 'updated_at')
    readonly_fields = ('updated_at',)

    def has_add_permission(self, request):
        return not BillingConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        'user', 'plan', 'status', 'interval',
        'current_period_end', 'cancel_at_period_end', 'updated_at',
    )
    list_filter = ('plan', 'status', 'interval', 'cancel_at_period_end')
    search_fields = ('user__username', 'user__email',
                     'stripe_customer_id', 'stripe_subscription_id')
    readonly_fields = ('created_at', 'updated_at')


class GenerationInline(admin.TabularInline):
    model = AIGeneration
    extra = 0
    can_delete = False
    readonly_fields = (
        'round_no', 'prompt', 'answer', 'prompt_tokens',
        'completion_tokens', 'success', 'error_message', 'created_at',
    )
    ordering = ('created_at',)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(AISession)
class AISessionAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'problem', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('user__username', 'user__email', 'problem__title')
    readonly_fields = ('created_at', 'updated_at')
    inlines = (GenerationInline,)


class ToolCallInline(admin.TabularInline):
    model = AIToolCall
    extra = 0
    can_delete = False
    readonly_fields = (
        'seq', 'tool_name', 'language', 'submission', 'status',
        'passed_cases', 'total_cases', 'runtime_ms', 'memory_kb',
        'error_message', 'result_json', 'created_at',
    )
    ordering = ('seq',)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(AIGeneration)
class AIGenerationAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'user', 'problem', 'session', 'round_no',
        'success', 'completion_tokens', 'created_at',
    )
    list_filter = ('success', 'round_no', 'created_at')
    search_fields = ('user__username', 'user__email', 'problem__title')
    readonly_fields = (
        'session', 'user', 'problem', 'round_no', 'prompt', 'answer',
        'prompt_tokens', 'completion_tokens', 'success',
        'error_message', 'created_at',
    )
    inlines = (ToolCallInline,)

    def has_add_permission(self, request):
        return False


@admin.register(AIToolCall)
class AIToolCallAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'generation', 'seq', 'tool_name', 'language',
        'status', 'passed_cases', 'total_cases', 'submission', 'created_at',
    )
    list_filter = ('status', 'tool_name', 'language', 'created_at')
    search_fields = (
        'generation__user__username', 'generation__problem__title',
        'submission__id',
    )
    readonly_fields = (
        'generation', 'seq', 'tool_name', 'language', 'code', 'submission',
        'status', 'passed_cases', 'total_cases', 'runtime_ms', 'memory_kb',
        'result_json', 'error_message', 'created_at',
    )

    def has_add_permission(self, request):
        return False
