from django.contrib import admin

from .models import PointConfig, PointLedgerEntry


@admin.register(PointConfig)
class PointConfigAdmin(admin.ModelAdmin):
    fieldsets = (
        ('邀请注册', {'fields': ('inviter_registration_points', 'invitee_registration_points')}),
        ('评测奖励', {'fields': ('accepted_testcase_points',)}),
    )
    readonly_fields = ('updated_at',)
    list_display = (
        'pk', 'inviter_registration_points', 'invitee_registration_points',
        'accepted_testcase_points', 'updated_at',
    )

    def has_add_permission(self, request):
        return not PointConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PointLedgerEntry)
class PointLedgerEntryAdmin(admin.ModelAdmin):
    list_display = ('user', 'amount', 'balance_after', 'event_type', 'description', 'created_at')
    list_filter = ('event_type', 'created_at')
    search_fields = ('user__username', 'event_key', 'description')
    readonly_fields = (
        'user', 'amount', 'balance_after', 'event_type', 'event_key', 'description', 'created_at',
    )
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in ('GET', 'HEAD') and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False
