# Generated for adding TOTP-based two-factor authentication to users.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0013_alter_user_points_balance'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='two_factor_secret',
            field=models.TextField(blank=True, default='', verbose_name='2FA 密钥（加密）'),
        ),
        migrations.AddField(
            model_name='user',
            name='two_factor_enabled',
            field=models.BooleanField(default=False, verbose_name='已启用 2FA'),
        ),
        migrations.AddField(
            model_name='user',
            name='two_factor_backup_codes',
            field=models.TextField(blank=True, default='', verbose_name='2FA 备用码哈希'),
        ),
        migrations.AddField(
            model_name='user',
            name='two_factor_setup_required',
            field=models.BooleanField(default=False, verbose_name='下次登录需重新设置 2FA'),
        ),
    ]
