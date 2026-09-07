import secrets

from django.contrib.auth.models import AbstractUser
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


def avatar_upload_to(instance, filename):
    """Legacy upload path retained for historical migrations."""
    return f'avatars/{instance.username}/{filename}'

class User(AbstractUser):
    email = models.EmailField(_('email address'), blank=True, unique=True)
    nickname = models.CharField(max_length=50, blank=True)
    bio = models.TextField(max_length=500, blank=True)
    avatar = models.ImageField(blank=True, null=True)
    # Django's implicit through model has indexed foreign keys and a unique
    # composite constraint, which supports leaderboard counts and duplicate-safe
    # `get_or_create` calls without a custom through model.
    solved_problems = models.ManyToManyField(
        'problems.Problem', blank=True, related_name='solved_by'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_login_ip = models.GenericIPAddressField(
        '最后登录 IP', blank=True, null=True,
    )
    points_balance = models.DecimalField('积分余额', max_digits=14, decimal_places=4, default=0)
    referral_code = models.CharField('邀请码', max_length=16, unique=True, blank=True)
    referrer = models.ForeignKey(
        'self', verbose_name='邀请人', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='referrals',
    )

    # --------- Punishment state (kept on User for fast, zero-lookups) -------
    is_permanently_banned = models.BooleanField('永久封号', default=False)
    banned_until = models.DateTimeField('临时封号截止时间', blank=True, null=True)
    banned_reason = models.CharField('封号原因', max_length=300, blank=True)
    # Comma-separated feature slugs. Example: `submit,comment,avatar,register`
    # — evaluated at request time by the enforcement middleware.
    disabled_features = models.TextField('禁用功能列表', blank=True, default='')
    disabled_features_until = models.DateTimeField(
        '功能禁用截止时间', blank=True, null=True
    )

    # --------- Two-factor authentication (TOTP, RFC 6238) -------------------
    # ``two_factor_secret`` stores the base32 secret encrypted at rest with
    # Fernet (key derived from ``settings.SECRET_KEY``). The field is only
    # populated while 2FA is enabled (or during the in-progress setup flow,
    # in which case ``two_factor_enabled`` is still False).
    two_factor_secret = models.TextField('2FA 密钥（加密）', blank=True, default='')
    two_factor_enabled = models.BooleanField('已启用 2FA', default=False)
    # Comma-separated SHA-256 hashes of one-time backup / scratch codes.
    # Plaintext codes are only ever shown once, at setup time.
    two_factor_backup_codes = models.TextField(
        '2FA 备用码哈希', blank=True, default='',
    )
    # Set when an admin forces the user to (re-)configure 2FA on next login.
    two_factor_setup_required = models.BooleanField(
        '下次登录需重新设置 2FA', default=False,
    )

    class Meta:
        ordering = ['-created_at']
        verbose_name = '用户'
        verbose_name_plural = '用户'

    def __str__(self):
        return self.username

    # --------- Helpers used by the admin UI / views ---------------------------
    @property
    def is_temporarily_banned(self) -> bool:
        if self.banned_until is None:
            return False
        return self.banned_until > timezone.now()

    @property
    def is_banned(self) -> bool:
        return bool(self.is_permanently_banned) or self.is_temporarily_banned

    def feature_disabled(self, feature: str) -> bool:
        """True when ``feature`` is explicitly disabled, and the deadline is
        still in the future (or none was set → permanent)."""
        if not feature:
            return False
        slug = feature.strip().lower()
        if not slug:
            return False
        disabled = {
            s.strip().lower()
            for s in (self.disabled_features or '').split(',')
            if s.strip()
        }
        if slug not in disabled:
            return False
        if self.disabled_features_until is None:
            return True
        return self.disabled_features_until > timezone.now()

    def punishment_info(self) -> dict:
        """Return a JSON-safe summary of the current punishment, suitable
        for rendering a modal. Returns ``None`` when the user has no active
        punishment.

        Shape::

            {
                "kind": "permanent_ban" | "temporary_ban" | "feature_ban",
                "title": "账号已被永久封号",            // popup 标题
                "reason": "管理员填写的原因 / 自动生成",  // popup 主体
                "ends_at": "2026-07-05 10:23:45" | null, // ISO-ish 本地时间
                "features": ["submit", "comment"],        // 被禁用的功能 slug
                "feature_labels": ["提交题目", "评论"],   // 对应 human label
            }
        """
        now = timezone.now()
        data = {
            'kind': None,
            'title': '',
            'reason': (self.banned_reason or '').strip() or '未填写具体原因',
            'ends_at': None,
            'features': [],
            'feature_labels': [],
        }
        # 1) Permanent ban — takes precedence over everything.
        if self.is_permanently_banned:
            data['kind'] = 'permanent_ban'
            data['title'] = '账号已被永久封禁'
            return data
        # 2) Temporary ban — if the ban is still active.
        if self.banned_until and self.banned_until > now:
            data['kind'] = 'temporary_ban'
            data['title'] = '账号已被临时封禁'
            ends_at_local = timezone.localtime(self.banned_until)
            data['ends_at'] = ends_at_local.strftime('%Y-%m-%d %H:%M:%S')
            return data
        # 3) Feature ban / warning — user can still log in but certain actions are blocked.
        feature_slugs = [
            s.strip() for s in (self.disabled_features or '').split(',') if s.strip()
        ]
        if feature_slugs and (
            self.disabled_features_until is None or self.disabled_features_until > now
        ):
            slug_to_label = {slug: label for slug, label in FEATURE_CHOICES}
            data['kind'] = 'feature_ban'
            data['title'] = '账号存在功能限制'
            data['features'] = feature_slugs
            data['feature_labels'] = [
                slug_to_label.get(s, s) for s in feature_slugs
            ]
            if self.disabled_features_until is not None:
                ends_at_local = timezone.localtime(self.disabled_features_until)
                data['ends_at'] = ends_at_local.strftime('%Y-%m-%d %H:%M:%S')
            return data
        # No active punishment.
        return None

    # --------- Two-factor authentication helpers ---------------------------
    @property
    def two_factor_required_for_user(self) -> bool:
        """True when this account *must* have 2FA enabled.

        Staff / superuser accounts are required to use 2FA. Standard users
        may opt in via the profile page.
        """
        return bool(self.is_staff or self.is_superuser)

    @property
    def has_two_factor(self) -> bool:
        """True when 2FA has been fully configured (secret set + enabled)."""
        return bool(self.two_factor_enabled and self.two_factor_secret)

    def two_factor_secret_plain(self) -> str:
        """Return the decrypted base32 secret, or '' if not configured."""
        if not self.two_factor_secret:
            return ''
        from .two_factor import decrypt_secret
        return decrypt_secret(self.two_factor_secret)

    def verify_two_factor_code(self, code: str) -> bool:
        """Verify a TOTP code against the stored secret."""
        secret = self.two_factor_secret_plain()
        if not secret or not code:
            return False
        from .two_factor import verify_code
        return verify_code(secret, code)

    def verify_two_factor_backup_code(self, code: str) -> bool:
        """Verify a one-time backup / scratch code."""
        if not code or not self.two_factor_backup_codes:
            return False
        from .two_factor import verify_backup_code
        return verify_backup_code(self.two_factor_backup_codes, code)

    def consume_two_factor_backup_code(self, code: str) -> bool:
        """If ``code`` matches a stored backup code, remove it and persist.
        Returns True on success, False otherwise."""
        if not code or not self.two_factor_backup_codes:
            return False
        from .two_factor import consume_backup_code
        remaining = consume_backup_code(self.two_factor_backup_codes, code)
        if remaining is None:
            return False
        self.two_factor_backup_codes = remaining
        self.save(update_fields=['two_factor_backup_codes'])
        return True

    @property
    def has_avatar(self) -> bool:
        return hasattr(self, 'avatar_blob')


    @property
    def avatar_url(self):
        if self.has_avatar:
            base = reverse('avatar', kwargs={'username': self.username})
            # Append a cache-busting timestamp so browsers fetch a fresh image
            # whenever the avatar is updated (the URL itself is otherwise stable).
            ts = int(self.avatar_blob.updated_at.timestamp())
            return f'{base}?v={ts}'
        return None

    def save(self, *args, **kwargs):
        if not self.referral_code:
            while True:
                candidate = secrets.token_urlsafe(8)[:12]
                if not User.objects.filter(referral_code=candidate).exists():
                    self.referral_code = candidate
                    break
        # Delete old avatar file if it's being changed
        if self.pk:
            try:
                old_user = User.objects.get(pk=self.pk)
                if old_user.avatar and old_user.avatar != self.avatar:
                    if default_storage.exists(old_user.avatar.name):
                        default_storage.delete(old_user.avatar.name)
            except User.DoesNotExist:
                pass

        # If avatar is being set to None/blank, delete the file
        if self.pk and not self.avatar:
            try:
                old_user = User.objects.get(pk=self.pk)
                if old_user.avatar:
                    if default_storage.exists(old_user.avatar.name):
                        default_storage.delete(old_user.avatar.name)
            except User.DoesNotExist:
                pass

        super().save(*args, **kwargs)
        # Clear only the bounded caches affected by user metadata.
        cache.delete('leaderboard_users')
        cache.delete('home_stats')  # Clear home stats cache


class AvatarBlob(models.Model):
    """Stores a user's uploaded avatar bytes directly in PostgreSQL."""
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='avatar_blob'
    )
    content_type = models.CharField(max_length=64)
    data = models.BinaryField()
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Avatar for {self.user.username}'

    def save(self, *args, **kwargs):
        # Avatar data is stored as uploaded. Image formats are already
        # compressed; gzip only adds CPU cost and complicates reads.
        # Invalidate cache when avatar is updated.
        cache_key = f'avatar_data:{self.user.username}'
        cache.delete(cache_key)
        super().save(*args, **kwargs)

    @property
    def image_data(self) -> bytes:
        """Return the uploaded image bytes."""
        return bytes(self.data)


# ---------------------------------------------------------------------------
# Punishment system
# ---------------------------------------------------------------------------
# Common feature slugs — referenced by User.disabled_features and by the
# enforcement middleware at request time. Keep slugs short, lowercase.
FEATURE_SUBMIT = 'submit'       # 提交题目
FEATURE_COMMENT = 'comment'      # 发表评论 / 回复 / 题解讨论
FEATURE_AVATAR = 'avatar'        # 修改头像 / 昵称
FEATURE_LOGIN = 'login'          # 禁用该账号登录（登录接口直接拒绝）
FEATURE_REGISTER = 'register'    # 保留：当前 IP 注册新账号被拒绝
FEATURE_NEWS = 'news'            # 发帖（预留）
FEATURE_CHAT = 'chat'            # 聊天（预留）
FEATURE_CREATE_PROBLEM = 'create_problem'  # 上传新题目 /problems/create/
FEATURE_ALL = '__all__'          # 临时全部禁用（除浏览）

FEATURE_CHOICES = (
    (FEATURE_SUBMIT, '禁止提交题目'),
    (FEATURE_COMMENT, '禁止发表评论 / 讨论'),
    (FEATURE_AVATAR, '禁止修改头像 / 昵称'),
    (FEATURE_LOGIN, '禁止登录'),
    (FEATURE_REGISTER, '禁止新账号注册（IP 级）'),
    (FEATURE_NEWS, '禁止发帖'),
    (FEATURE_CHAT, '禁止使用聊天'),
    (FEATURE_CREATE_PROBLEM, '禁止上传新题目'),
)


class UserPunishment(models.Model):
    """Permanent / time-boxed punishment record for a user.

    Creating a ``UserPunishment`` automatically updates the matching
    ``User`` fields (``banned_until``, ``disabled_features``, etc.) —
    so enforcement at request-time is a single User-row lookup.
    """

    TYPE_PERMANENT_BAN = 'permanent_ban'
    TYPE_TEMP_BAN = 'temp_ban'
    TYPE_FEATURE = 'feature_ban'
    TYPE_WARNING = 'warning'

    TYPE_CHOICES = (
        (TYPE_PERMANENT_BAN, '永久封号'),
        (TYPE_TEMP_BAN, '临时封号'),
        (TYPE_FEATURE, '禁用某些功能'),
        (TYPE_WARNING, '警告'),
    )

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='punishments'
    )
    punishment_type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    reason = models.CharField(max_length=500, blank=True)
    # Temp-ban window (both for full bans and feature bans)
    duration_days = models.IntegerField(null=True, blank=True)
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(null=True, blank=True)
    # For TYPE_FEATURE: comma-separated slugs matching FEATURE_CHOICES above
    disabled_features = models.CharField(max_length=300, blank=True, default='')
    # For TYPE_WARNING: a short public note (optional).
    note = models.CharField(max_length=200, blank=True)

    created_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='punishments_issued'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='punishments_revoked'
    )
    revoked_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = '用户处罚记录'
        verbose_name_plural = '用户处罚记录'

    def __str__(self) -> str:
        return (
            f'[{self.get_punishment_type_display()}] '
            f'{self.user.username} ({self.reason[:40] or "-"})'
        )

    # ---- Core behaviour: apply to the User row on save -----------------
    def apply_to_user(self, *, created: bool) -> None:
        """Mirror this punishment onto the matching ``User`` fields."""
        if self.revoked_at:
            return

        now = timezone.now()
        if self.punishment_type == self.TYPE_PERMANENT_BAN:
            self.user.is_permanently_banned = True
            self.user.banned_reason = self.reason or self.user.banned_reason
        elif self.punishment_type == self.TYPE_TEMP_BAN:
            new_end = self.starts_at + timezone.timedelta(
                days=self.duration_days or 1
            )
            if self.user.banned_until is None or self.user.banned_until < new_end:
                self.user.banned_until = new_end
            self.user.banned_reason = self.reason or self.user.banned_reason
        elif self.punishment_type == self.TYPE_FEATURE:
            existing = {
                s.strip() for s in (self.user.disabled_features or '').split(',')
                if s.strip()
            }
            for s in (self.disabled_features or '').split(','):
                s = s.strip()
                if s:
                    existing.add(s)
            self.user.disabled_features = ','.join(sorted(existing))
            if self.duration_days:
                new_end = self.starts_at + timezone.timedelta(
                    days=self.duration_days
                )
                if (self.user.disabled_features_until is None
                        or self.user.disabled_features_until < new_end):
                    self.user.disabled_features_until = new_end
            self.user.banned_reason = self.reason or self.user.banned_reason
        self.user.save(update_fields=[
            'is_permanently_banned',
            'banned_until',
            'banned_reason',
            'disabled_features',
            'disabled_features_until',
        ])

    def save(self, *args, **kwargs):
        created = self.pk is None
        super().save(*args, **kwargs)
        if created:
            try:
                self.apply_to_user(created=True)
            except Exception:
                pass  # best effort; admin UI can re-apply manually


class IpBan(models.Model):
    """IP-level ban. Evaluated by the enforcement middleware *before*
    any authenticated user lookup, so it blocks unauthenticated traffic
    as well.
    """

    ip_address = models.CharField(max_length=64, unique=True)
    reason = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    is_permanent = models.BooleanField(default=False)
    notes = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'IP 封禁'
        verbose_name_plural = 'IP 封禁'

    def __str__(self) -> str:
        if self.is_permanent:
            return f'[PERM] {self.ip_address}'
        return f'[TEMP] {self.ip_address} until {self.ends_at}'

    @property
    def is_active(self) -> bool:
        if self.is_permanent:
            return True
        if self.ends_at is None:
            return True
        return self.ends_at > timezone.now()
