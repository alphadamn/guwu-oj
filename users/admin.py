from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.db.models import Count
from django.utils.translation import gettext_lazy as _

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = (
        'username',
        'email',
        'nickname',
        'is_staff',
        'is_active',
        'solved_count',
        'submission_count',
        'date_joined',
        'created_at',
    )
    list_filter = ('is_staff', 'is_superuser', 'is_active', 'date_joined', 'created_at')
    search_fields = ('username', 'email', 'nickname', 'first_name', 'last_name')
    ordering = ('-created_at',)
    filter_horizontal = ('groups', 'user_permissions', 'solved_problems')
    readonly_fields = (
        'created_at',
        'last_login',
        'date_joined',
        'solved_count_display',
        'submission_count_display',
    )
    actions = ['activate_users', 'deactivate_users', 'grant_staff', 'revoke_staff']

    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        (_('个人信息'), {
            'fields': ('email', 'nickname', 'first_name', 'last_name', 'bio', 'avatar'),
        }),
        (_('权限'), {
            'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions'),
        }),
        (_('统计'), {
            'fields': (
                'solved_count_display',
                'submission_count_display',
                'created_at',
                'last_login',
                'date_joined',
            ),
        }),
        (_('已通过题目'), {
            'fields': ('solved_problems',),
            'classes': ('collapse',),
        }),
    )
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': (
                'username',
                'email',
                'password1',
                'password2',
                'nickname',
                'is_staff',
                'is_active',
            ),
        }),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(
            _submission_count=Count('submissions', distinct=True),
            _solved_count=Count('solved_problems', distinct=True),
        )

    @admin.display(description='提交数', ordering='_submission_count')
    def submission_count(self, obj):
        if hasattr(obj, '_submission_count'):
            return obj._submission_count
        return obj.submissions.count()

    @admin.display(description='通过题数', ordering='_solved_count')
    def solved_count(self, obj):
        if hasattr(obj, '_solved_count'):
            return obj._solved_count
        return obj.solved_problems.count()

    @admin.display(description='提交数')
    def submission_count_display(self, obj):
        return self.submission_count(obj)

    @admin.display(description='通过题数')
    def solved_count_display(self, obj):
        return self.solved_count(obj)

    @admin.action(description='启用选中的用户')
    def activate_users(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f'已启用 {updated} 个用户。')

    @admin.action(description='禁用选中的用户')
    def deactivate_users(self, request, queryset):
        updated = queryset.exclude(pk=request.user.pk).update(is_active=False)
        self.message_user(request, f'已禁用 {updated} 个用户。')

    @admin.action(description='设为管理员')
    def grant_staff(self, request, queryset):
        updated = queryset.update(is_staff=True)
        self.message_user(request, f'已将 {updated} 个用户设为管理员。')

    @admin.action(description='取消管理员')
    def revoke_staff(self, request, queryset):
        updated = queryset.exclude(pk=request.user.pk).update(is_staff=False)
        self.message_user(request, f'已取消 {updated} 个用户的管理员权限。')
