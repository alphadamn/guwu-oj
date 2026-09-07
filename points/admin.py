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
        # 审计记录：禁止在本应用自身的 admin 页面直接删除（列表动作、删除
        # 按钮、删除视图）；但删除用户等级联删除时请求目标是其它模型
        # （如 users_user_delete / users_user_changelist），此时回落到
        # 默认权限判断——否则无条件返回 False 会让级联对象进入
        # perms_needed，连超级用户都无法完成账号删除。
        match = getattr(request, 'resolver_match', None)
        if match is not None and match.url_name.startswith(
            f'{self.model._meta.app_label}_'
        ):
            return False
        return super().has_delete_permission(request, obj)


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
        # 审计记录：禁止在本应用自身的 admin 页面直接删除（列表动作、删除
        # 按钮、删除视图）；但删除用户等级联删除时请求目标是其它模型
        # （如 users_user_delete / users_user_changelist），此时回落到
        # 默认权限判断——否则无条件返回 False 会让级联对象进入
        # perms_needed，连超级用户都无法完成账号删除。
        match = getattr(request, 'resolver_match', None)
        if match is not None and match.url_name.startswith(
            f'{self.model._meta.app_label}_'
        ):
            return False
        return super().has_delete_permission(request, obj)
