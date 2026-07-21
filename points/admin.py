from django.contrib import admin

from .models import DailyCheckIn, PointConfig, PointLedgerEntry


@admin.register(PointConfig)
class PointConfigAdmin(admin.ModelAdmin):
    fieldsets = (
        ('邀请注册', {'fields': ('inviter_registration_points', 'invitee_registration_points')}),
        ('评测奖励', {'fields': ('accepted_testcase_points',)}),
        ('每日签到奖励', {'fields': (
            'daily_checkin_day_1_points', 'daily_checkin_day_2_points',
            'daily_checkin_day_3_points', 'daily_checkin_day_4_points',
            'daily_checkin_day_5_plus_points',
        )}),
    )
    readonly_fields = ('updated_at',)
    list_display = (
        'pk', 'inviter_registration_points', 'invitee_registration_points',
        'accepted_testcase_points', 'daily_checkin_day_1_points',
        'daily_checkin_day_2_points', 'daily_checkin_day_3_points',
        'daily_checkin_day_4_points', 'daily_checkin_day_5_plus_points', 'updated_at',
    )

    def has_add_permission(self, request):
        return not PointConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DailyCheckIn)
class DailyCheckInAdmin(admin.ModelAdmin):
    list_display = ('user', 'day', 'streak', 'points_awarded', 'created_at')
    list_filter = ('day',)
    search_fields = ('user__username', 'user__email')
    readonly_fields = ('user', 'day', 'streak', 'points_awarded', 'created_at')
    date_hierarchy = 'day'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in ('GET', 'HEAD') and super().has_change_permission(request, obj)

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
