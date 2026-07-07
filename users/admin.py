from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.db.models import Count
from django.utils.translation import gettext_lazy as _

from .models import User, UserPunishment, IpBan


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------
# Fields that can lead to privilege escalation. Only superusers may modify
# these directly. (Django's auth groups / user_permissions also fall into
# this category because they could be used to grant dangerous permissions.)
PRIVILEGE_ESCALATION_FIELDS = (
    'is_staff', 'is_superuser', 'groups', 'user_permissions',
)


def _is_superuser(request) -> bool:
    return bool(getattr(request, 'user', None) and request.user.is_active
                and request.user.is_superuser)


def _has_userpunishment_perm(request, action: str = 'change') -> bool:
    """Check if current user has users.{action}_userpunishment."""
    return bool(getattr(request, 'user', None) and request.user.is_active
                and request.user.has_perm(f'users.{action}_userpunishment'))


def _has_ipban_perm(request, action: str = 'change') -> bool:
    """Check if current user has users.{action}_ipban."""
    return bool(getattr(request, 'user', None) and request.user.is_active
                and request.user.has_perm(f'users.{action}_ipban'))


# ---------------------------------------------------------------------------
# User admin
# ---------------------------------------------------------------------------

@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = (
        'username',
        'email',
        'nickname',
        'is_staff',
        'is_active',
        'ban_status',
        'solved_count',
        'submission_count',
        'date_joined',
        'created_at',
    )
    list_filter = (
        'is_staff', 'is_superuser', 'is_active',
        'is_permanently_banned', 'date_joined', 'created_at',
    )
    search_fields = (
        'username', 'email', 'nickname', 'first_name', 'last_name',
        'banned_reason',
    )
    ordering = ('-created_at',)
    filter_horizontal = (
        'groups', 'user_permissions', 'solved_problems',
    )
    readonly_fields = (
        'created_at',
        'last_login',
        'date_joined',
        'solved_count_display',
        'submission_count_display',
    )
    actions = [
        'activate_users', 'deactivate_users',
        'grant_staff', 'revoke_staff',
        'ban_permanently', 'ban_7_days', 'ban_30_days', 'unban',
        'disable_submissions_7_days',
    ]

    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        (_('个人信息'), {
            'fields': ('email', 'nickname', 'first_name', 'last_name', 'bio', 'avatar'),
        }),
        (_('权限'), {
            'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions'),
        }),
        (_('惩罚 / 限制'), {
            'fields': (
                'is_permanently_banned',
                'banned_until',
                'banned_reason',
                'disabled_features',
                'disabled_features_until',
            ),
            'classes': ('collapse',),
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

    # ------ Security overrides ----------------------------------------------
    def get_readonly_fields(self, request, obj=None):
        base = tuple(super().get_readonly_fields(request, obj))
        extra = []

        # 1. Privilege-escalation fields: only superusers can edit.
        if not _is_superuser(request):
            extra.extend(PRIVILEGE_ESCALATION_FIELDS)

        # 2. Punishment fields on User: editable if user has
        #    users.delete_userpunishment permission (unban = delete/撤销).
        can_unban = _is_superuser(request) or _has_userpunishment_perm(request, 'delete')
        if not can_unban:
            extra.extend([
                'is_permanently_banned', 'banned_until', 'banned_reason',
                'disabled_features', 'disabled_features_until',
            ])

        return base + tuple(extra)

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if _is_superuser(request):
            return fieldsets
        # Hide the "password" reset field for non-superusers on existing
        # users so they cannot reset another user's password via the admin.
        result = []
        for title, conf in fieldsets:
            new_fields = []
            for f in conf.get('fields', ()):
                if isinstance(f, (list, tuple)):
                    cleaned = tuple(x for x in f if x != 'password')
                    new_fields.append(cleaned if cleaned else f)
                elif f == 'password':
                    continue
                else:
                    new_fields.append(f)
            if new_fields:
                result.append((title, {**conf, 'fields': tuple(new_fields)}))
        return result

    def get_actions(self, request):
        actions = super().get_actions(request)

        # 1. Privilege-escalation actions: only superusers.
        privilege_actions = {'grant_staff', 'revoke_staff'}
        # 2. Actions that create new punishment records (needs add).
        add_punishment_actions = {
            'ban_permanently', 'ban_7_days', 'ban_30_days',
            'disable_submissions_7_days',
        }
        # 3. Action that clears punishment status (needs delete).
        unban_action = {'unban'}

        if not _is_superuser(request):
            for key in privilege_actions:
                actions.pop(key, None)

        can_add_punishment = (
            _is_superuser(request)
            or _has_userpunishment_perm(request, 'add')
        )
        if not can_add_punishment:
            for key in add_punishment_actions:
                actions.pop(key, None)

        can_delete_punishment = (
            _is_superuser(request)
            or _has_userpunishment_perm(request, 'delete')
        )
        if not can_delete_punishment:
            for key in unban_action:
                actions.pop(key, None)

        return actions

    def has_change_permission(self, request, obj=None):
        if not super().has_change_permission(request, obj):
            return False
        if _is_superuser(request):
            return True
        # Non-superusers cannot edit a superuser at all.
        if obj is not None and getattr(obj, 'is_superuser', False):
            return False
        return True

    def save_model(self, request, obj, form, change):
        if _is_superuser(request):
            super().save_model(request, obj, form, change)
            return

        # Non-superusers: silently drop any attempt to modify privilege-
        # escalation fields in case get_readonly_fields was somehow bypassed
        # (e.g. a crafted POST). We re-read the DB values for those fields.
        if change and obj.pk:
            try:
                original = User.objects.only(*PRIVILEGE_ESCALATION_FIELDS).get(pk=obj.pk)
                for field in PRIVILEGE_ESCALATION_FIELDS:
                    setattr(obj, field, getattr(original, field))
            except User.DoesNotExist:
                pass
        else:
            # New users created by non-superusers: never grant staff.
            obj.is_staff = False
            obj.is_superuser = False
        super().save_model(request, obj, form, change)

    # ------ Helpers ---------------------------------------------------------
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(
            _submission_count=Count('submissions', distinct=True),
            _solved_count=Count('solved_problems', distinct=True),
        )

    @admin.display(description='封禁状态', boolean=False)
    def ban_status(self, obj):
        if getattr(obj, 'is_permanently_banned', False):
            return '永久封禁'
        if getattr(obj, 'banned_until', None):
            return f'临时封禁至 {obj.banned_until:%Y-%m-%d %H:%M}'
        if obj.disabled_features:
            return f'限制功能: {obj.disabled_features}'
        return '正常'

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

    # ------ Actions ---------------------------------------------------------
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
        if not _is_superuser(request):
            self.message_user(request, '仅超级用户可执行此操作。', level=messages.ERROR)
            return
        updated = queryset.update(is_staff=True)
        self.message_user(request, f'已将 {updated} 个用户设为管理员。')

    @admin.action(description='取消管理员')
    def revoke_staff(self, request, queryset):
        if not _is_superuser(request):
            self.message_user(request, '仅超级用户可执行此操作。', level=messages.ERROR)
            return
        updated = queryset.exclude(pk=request.user.pk).update(is_staff=False)
        self.message_user(request, f'已取消 {updated} 个用户的管理员权限。')

    # ------ Punishment bulk actions ----------------------------------------
    def _create_punishments(self, request, queryset, kind, days, feature):
        from django.utils import timezone
        created = 0
        for u in queryset.exclude(pk=request.user.pk):
            punishment = UserPunishment(
                user=u,
                punishment_type=kind,
                reason='管理员批量操作',
                duration_days=days,
                starts_at=timezone.now(),
                ends_at=(timezone.now() + timezone.timedelta(days=days)) if days else None,
                disabled_features=feature or '',
                created_by=request.user,
            )
            try:
                punishment.save()
                created += 1
            except Exception:
                continue
        self.message_user(request, f'已对 {created} 个用户执行惩罚。')

    @admin.action(description='永久封禁')
    def ban_permanently(self, request, queryset):
        if not (_is_superuser(request) or _has_userpunishment_perm(request, 'add')):
            self.message_user(request, '需要用户处罚权限才能执行此操作。', level=messages.ERROR)
            return
        self._create_punishments(
            request, queryset,
            kind=UserPunishment.TYPE_PERMANENT_BAN, days=None, feature='',
        )

    @admin.action(description='临时封禁 7 天')
    def ban_7_days(self, request, queryset):
        if not (_is_superuser(request) or _has_userpunishment_perm(request, 'add')):
            self.message_user(request, '需要用户处罚权限才能执行此操作。', level=messages.ERROR)
            return
        self._create_punishments(
            request, queryset,
            kind=UserPunishment.TYPE_TEMP_BAN, days=7, feature='',
        )

    @admin.action(description='临时封禁 30 天')
    def ban_30_days(self, request, queryset):
        if not (_is_superuser(request) or _has_userpunishment_perm(request, 'add')):
            self.message_user(request, '需要用户处罚权限才能执行此操作。', level=messages.ERROR)
            return
        self._create_punishments(
            request, queryset,
            kind=UserPunishment.TYPE_TEMP_BAN, days=30, feature='',
        )

    @admin.action(description='解除封禁 / 恢复提交权限')
    def unban(self, request, queryset):
        if not (_is_superuser(request) or _has_userpunishment_perm(request, 'delete')):
            self.message_user(request, '需要用户处罚删除权限才能执行此操作。', level=messages.ERROR)
            return
        updated = queryset.exclude(pk=request.user.pk).update(
            is_permanently_banned=False,
            banned_until=None,
            banned_reason='',
            disabled_features='',
            disabled_features_until=None,
        )
        self.message_user(request, f'已解除 {updated} 个用户的封禁/限制。')

    @admin.action(description='禁止提交题目 7 天')
    def disable_submissions_7_days(self, request, queryset):
        if not (_is_superuser(request) or _has_userpunishment_perm(request, 'add')):
            self.message_user(request, '需要用户处罚权限才能执行此操作。', level=messages.ERROR)
            return
        self._create_punishments(
            request, queryset,
            kind=UserPunishment.TYPE_FEATURE, days=7, feature='submit',
        )


# ---------------------------------------------------------------------------
# UserPunishment admin — governed by Django auth permissions
# ---------------------------------------------------------------------------

class UserPunishmentAdmin(admin.ModelAdmin):
    list_display = (
        'user', 'punishment_type', 'duration_days', 'starts_at',
        'ends_at', 'created_by', 'created_at',
    )
    list_filter = ('punishment_type', 'starts_at', 'ends_at')
    search_fields = (
        'user__username', 'user__email', 'reason', 'disabled_features',
    )
    readonly_fields = ('created_at',)
    fieldsets = (
        (None, {
            'fields': (
                'user', 'punishment_type', 'reason', 'note',
                'duration_days', 'starts_at', 'ends_at',
                'disabled_features',
                'created_by', 'created_at',
                'revoked_at', 'revoked_by', 'revoked_reason',
            ),
        }),
    )


admin.site.register(UserPunishment, UserPunishmentAdmin)


# ---------------------------------------------------------------------------
# IpBan admin — governed by Django auth permissions
# ---------------------------------------------------------------------------

class IpBanAdmin(admin.ModelAdmin):
    list_display = (
        'ip_address', 'is_permanent', 'ends_at', 'is_active',
        'created_by', 'created_at',
    )
    list_filter = ('is_permanent', 'created_at', 'ends_at')
    search_fields = ('ip_address', 'reason', 'notes')
    readonly_fields = ('created_at',)
    fieldsets = (
        (None, {
            'fields': ('ip_address', 'is_permanent', 'ends_at', 'reason',
                       'notes', 'created_by', 'created_at'),
        }),
    )


admin.site.register(IpBan, IpBanAdmin)
