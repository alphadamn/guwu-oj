from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('points', '0004_alter_pointconfig_accepted_testcase_points_and_more')]

    operations = [
        migrations.AddField(
            model_name='pointconfig', name='daily_checkin_day_1_points',
            field=models.PositiveIntegerField(default=10, verbose_name='连续签到第 1 天积分'),
        ),
        migrations.AddField(
            model_name='pointconfig', name='daily_checkin_day_2_points',
            field=models.PositiveIntegerField(default=15, verbose_name='连续签到第 2 天积分'),
        ),
        migrations.AddField(
            model_name='pointconfig', name='daily_checkin_day_3_points',
            field=models.PositiveIntegerField(default=30, verbose_name='连续签到第 3 天积分'),
        ),
        migrations.AddField(
            model_name='pointconfig', name='daily_checkin_day_4_points',
            field=models.PositiveIntegerField(default=50, verbose_name='连续签到第 4 天积分'),
        ),
        migrations.AddField(
            model_name='pointconfig', name='daily_checkin_day_5_plus_points',
            field=models.PositiveIntegerField(default=75, verbose_name='连续签到第 5 天及以上积分'),
        ),
        migrations.CreateModel(
            name='DailyCheckIn',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('day', models.DateField(verbose_name='签到日期')),
                ('streak', models.PositiveIntegerField(verbose_name='连续签到天数')),
                ('points_awarded', models.DecimalField(decimal_places=4, max_digits=14, verbose_name='获得积分')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='daily_checkins', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-day', '-id'], 'verbose_name': '每日签到', 'verbose_name_plural': '每日签到'},
        ),
        migrations.AddConstraint(
            model_name='dailycheckin',
            constraint=models.UniqueConstraint(fields=('user', 'day'), name='unique_user_daily_checkin'),
        ),
    ]
