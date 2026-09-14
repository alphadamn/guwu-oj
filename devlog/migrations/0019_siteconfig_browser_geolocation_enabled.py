from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('devlog', '0019_rename_devlog_tra_day_lat_long_idx_devlog_traf_day_160530_idx'),
    ]

    operations = [
        migrations.AddField(
            model_name='siteconfig',
            name='browser_geolocation_enabled',
            field=models.BooleanField(
                default=True,
                help_text='开启后，在用户明确同意分析且浏览器授权时优先使用浏览器位置；否则使用 IP GeoLite2 定位。',
                verbose_name='优先使用浏览器定位',
            ),
        ),
    ]


# Force-add this migration because the repository ignores migrations globally.
