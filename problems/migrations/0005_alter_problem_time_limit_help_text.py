# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('problems', '0004_solution'),
    ]

    operations = [
        migrations.AlterField(
            model_name='problem',
            name='memory_limit',
            field=models.IntegerField(default=256, help_text='内存限制（MB）'),
        ),
        migrations.AlterField(
            model_name='problem',
            name='time_limit',
            field=models.IntegerField(
                default=1000,
                help_text='时间限制（毫秒）。评测程序会按毫秒换算为秒。',
            ),
        ),
    ]
