from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('devlog', '0014_trafficcountrymetric')]
    operations = [
        migrations.RenameIndex(
            model_name='trafficcountrymetric',
            old_name='devlog_traf_day_8b7f8a_idx',
            new_name='devlog_traf_day_5263c3_idx',
        ),
    ]
