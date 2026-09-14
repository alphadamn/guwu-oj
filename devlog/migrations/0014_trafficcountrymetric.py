from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('devlog', '0013_trafficpagemetric')]
    operations = [
        migrations.CreateModel(
            name='TrafficCountryMetric',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('day', models.DateField(db_index=True, verbose_name='日期')),
                ('country_code', models.CharField(max_length=2, verbose_name='国家代码')),
                ('country_name', models.CharField(max_length=100, verbose_name='国家')),
                ('latitude', models.FloatField(verbose_name='纬度')),
                ('longitude', models.FloatField(verbose_name='经度')),
                ('requests', models.PositiveBigIntegerField(default=0, verbose_name='请求数')),
            ],
            options={'ordering': ['-day', '-requests', 'country_code'], 'verbose_name': '每日国家流量', 'verbose_name_plural': '每日国家流量'},
        ),
        migrations.AddConstraint(
            model_name='trafficcountrymetric',
            constraint=models.UniqueConstraint(fields=('day', 'country_code'), name='unique_traffic_country_day'),
        ),
        migrations.AddIndex(
            model_name='trafficcountrymetric',
            index=models.Index(fields=['day', 'country_code'], name='devlog_traf_day_8b7f8a_idx'),
        ),
    ]
