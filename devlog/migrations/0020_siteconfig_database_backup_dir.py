from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('devlog', '0019_siteconfig_browser_geolocation_enabled'),
    ]

    operations = [
        migrations.AddField(
            model_name='siteconfig',
            name='database_backup_dir',
            field=models.CharField(
                blank=True,
                default='',
                help_text='非 HTTPS 访问时，备份文件写入该目录；导入时也从该目录列出可选备份。'
                          '留空则使用项目下的 backups/database/。目录必须可被运行 Django 的用户读写。',
                max_length=500,
                verbose_name='数据库备份目录（服务器路径）',
            ),
        ),
    ]
