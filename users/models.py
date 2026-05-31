import uuid
from pathlib import Path

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _


def avatar_upload_to(instance, filename):
    ext = Path(filename).suffix.lower() or '.jpg'
    return f'avatars/{uuid.uuid4().hex}{ext}'


class User(AbstractUser):
    email = models.EmailField(_('email address'), blank=True, unique=True)
    nickname = models.CharField(max_length=50, blank=True)
    bio = models.TextField(max_length=500, blank=True)
    avatar = models.ImageField(upload_to=avatar_upload_to, blank=True, null=True)
    solved_problems = models.ManyToManyField(
        'problems.Problem', blank=True, related_name='solved_by'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return self.username
