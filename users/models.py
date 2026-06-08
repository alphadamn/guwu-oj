import gzip
import uuid
from pathlib import Path
import os

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.core.cache import cache
from django.core.files.storage import default_storage

class User(AbstractUser):
    email = models.EmailField(_('email address'), blank=True, unique=True)
    nickname = models.CharField(max_length=50, blank=True)
    bio = models.TextField(max_length=500, blank=True)
    avatar = models.ImageField(blank=True, null=True)
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
        cache.delete('leaderboard_users')  # Clear leaderboard cache
        cache.delete('home_stats')  # Clear home stats cache


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

    @classmethod
    def compress(cls, data: bytes) -> bytes:
        """Compress data using gzip."""
        return gzip.compress(data)

    def __str__(self):
        return f'Avatar for {self.user.username}'

    def save(self, *args, **kwargs):
        # Compress data before saving
        if self.data[:2].hex() != b'\x1f\x8b'.hex():
            self.data = self.compress(self.data)
        
        # Invalidate cache when avatar is updated
        cache_key = f'avatar_data:{self.user.username}'
        freq_key_pattern = f'avatar_freq:{self.user.username}:*'
        cache.delete(cache_key)
        cache.delete_pattern(freq_key_pattern)
        
        super().save(*args, **kwargs)

    @property
    def image_data(self) -> bytes:
        """Decompress data when reading."""
        # Convert memory object to bytes if needed
        data = bytes(self.data)

        # Debug logging
        gzip_magic = b'\x1f\x8b'

        # Check if data is already uncompressed (gzip magic number is 0x1f 0x8b)
        if len(data) >= 2 and data[:2] != gzip_magic:
            return data

        try:
            decompressed = gzip.decompress(data)
            with open('tmp.png', 'wb') as f:
                f.write(decompressed)
            return decompressed
        except Exception as e:
            return data
