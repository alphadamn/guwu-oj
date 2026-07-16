from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('submissions', '0008_alter_submission_language'),
    ]

    operations = [
        migrations.AlterField(
            model_name='submission',
            name='status',
            field=models.CharField(
                choices=[
                    ('Pending', 'Pending'),
                    ('Accepted', 'Accepted'),
                    ('Wrong Answer', 'Wrong Answer'),
                    ('Time Limit Exceeded', 'Time Limit Exceeded'),
                    ('Memory Limit Exceeded', 'Memory Limit Exceeded'),
                    ('Runtime Error', 'Runtime Error'),
                    ('Compile Error', 'Compile Error'),
                    ('System Error', 'System Error'),
                ],
                default='Pending',
                max_length=30,
            ),
        ),
    ]
