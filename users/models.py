import uuid
from pathlib import Path
import os

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.core.cache import cache
from django.core.files.storage import default_storage


def avatar_upload_to(instance, filename):
    ext = Path(filename).suffix.lower() or '.jpg'
    return f'avatars/{uuid.uuid4().hex}{ext}'


class User(AbstractUser):
    email = models.EmailField(_('email address'), blank=True, unique=True)
    nickname = models.CharField(max_length=50, blank=True)
    bio = models.TextField(max_length=500, blank=True)
    # Legacy file-based avatar field. Kept for migration compatibility, but new
    # uploads are stored as raw bytes in the `AvatarBlob` table (PostgreSQL).
    avatar = models.ImageField(upload_to=avatar_upload_to, blank=True, null=True)
    solved_problems = models.ManyToManyField(
        'problems.Problem', blank=True, related_name='solved_by'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.username

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
        # Clear relevant caches when user is saved
        cache.delete_pattern('views.decorators.cache.*')  # Clear all view caches


class AvatarBlob(models.Model):
    """
    Stores a user's avatar as raw bytes directly in PostgreSQL.
    Served via the `avatar` view (users:avatar) – not via static/media files.
    """
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='avatar_blob'
    )
    content_type = models.CharField(max_length=64)
    data = models.BinaryField()
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Avatar for {self.user.username}'
