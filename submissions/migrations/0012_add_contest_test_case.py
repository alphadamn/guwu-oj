import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('contests', '0003_standalone_contest_problem_schema'),
        ('submissions', '0011_standalone_contest_submission_support'),
    ]

    operations = [
        migrations.AddField(
            model_name='submissiontestresult',
            name='contest_test_case',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                to='contests.contesttestcase',
            ),
        ),
    ]
