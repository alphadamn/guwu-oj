from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import ValidationError
from PIL import Image

from .models import User


class UserRegisterForm(UserCreationForm):
    email = forms.EmailField(required=True)
    nickname = forms.CharField(max_length=50, required=False)
    password1 = forms.CharField(
        label='密码',
        strip=False,
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
    )
    password2 = forms.CharField(
        label='确认密码',
        strip=False,
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
    )

    class Meta:
        model = User
        fields = ('username', 'email', 'nickname', 'password1', 'password2')

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError('该邮箱已被注册。')
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data['email']
        user.nickname = self.cleaned_data.get('nickname', '')
        if commit:
            user.save()
        return user


class UserUpdateForm(forms.ModelForm):
    MAX_AVATAR_SIZE = 5 * 1024 * 1024  # 2MB
    ALLOWED_CONTENT_TYPES = {'image/jpeg', 'image/png', 'image/webp', 'image/gif'}

    class Meta:
        model = User
        fields = ('nickname', 'bio', 'avatar')
        widgets = {
            'bio': forms.Textarea(attrs={'rows': 4}),
        }

    def clean_avatar(self):
        avatar = self.cleaned_data.get('avatar')
        if not avatar:
            return avatar

        if avatar.size > self.MAX_AVATAR_SIZE:
            raise ValidationError('头像大小不能超过 5MB。')

        content_type = getattr(avatar, 'content_type', '')
        if content_type not in self.ALLOWED_CONTENT_TYPES:
            raise ValidationError('仅支持 JPG/PNG/WEBP/GIF 格式头像。')

        try:
            avatar.seek(0)
            img = Image.open(avatar)
            img.verify()
            avatar.seek(0)
        except Exception as exc:
            raise ValidationError('上传文件不是有效图片。') from exc

        return avatar
