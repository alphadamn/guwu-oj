from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('contests', '0002_reset_pointer_based_contest_data'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveConstraint(model_name='contestproblem', name='unique_contest_problem'),
            ],
        ),
        migrations.RemoveField(model_name='contestproblem', name='problem'),
        migrations.AddField(model_name='contestproblem', name='created_at', field=models.DateTimeField(auto_now_add=True, null=True)),
        migrations.AddField(model_name='contestproblem', name='created_by', field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name='created_contest_problems', to=settings.AUTH_USER_MODEL)),
        migrations.AddField(model_name='contestproblem', name='description', field=models.TextField(default=''), preserve_default=False),
        migrations.AddField(model_name='contestproblem', name='difficulty', field=models.CharField(choices=[('入门', '入门'), ('普及-', '普及-'), ('普及', '普及'), ('普及+', '普及+'), ('提高-', '提高-'), ('提高', '提高'), ('提高+', '提高+'), ('省选', '省选'), ('NOI', 'NOI')], default='普及', max_length=10)),
        migrations.AddField(model_name='contestproblem', name='hint', field=models.TextField(blank=True)),
        migrations.AddField(model_name='contestproblem', name='input_format', field=models.TextField(default=''), preserve_default=False),
        migrations.AddField(model_name='contestproblem', name='memory_limit', field=models.IntegerField(default=256)),
        migrations.AddField(model_name='contestproblem', name='output_format', field=models.TextField(default=''), preserve_default=False),
        migrations.AddField(model_name='contestproblem', name='published_problem', field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='published_from_contest_problem', to='problems.problem')),
        migrations.AddField(model_name='contestproblem', name='sample_input', field=models.TextField(blank=True)),
        migrations.AddField(model_name='contestproblem', name='sample_output', field=models.TextField(blank=True)),
        migrations.AddField(model_name='contestproblem', name='tags', field=models.CharField(blank=True, max_length=200)),
        migrations.AddField(model_name='contestproblem', name='time_limit', field=models.IntegerField(default=1000)),
        migrations.AddField(model_name='contestproblem', name='title', field=models.CharField(default='', max_length=200), preserve_default=False),
        migrations.AddField(model_name='contestproblem', name='updated_at', field=models.DateTimeField(auto_now=True, null=True)),
        migrations.CreateModel(
            name='ContestTestCase',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('input_data', models.TextField(blank=True)),
                ('expected_output', models.TextField()),
                ('order', models.PositiveIntegerField(default=0)),
                ('is_sample', models.BooleanField(default=False)),
                ('contest_problem', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='test_cases', to='contests.contestproblem')),
            ],
            options={'ordering': ['order', 'id']},
        ),
        migrations.AddConstraint(model_name='contestproblem', constraint=models.UniqueConstraint(fields=('contest', 'order'), name='unique_contest_problem_order')),
        migrations.AlterField(model_name='contestproblem', name='created_at', field=models.DateTimeField(auto_now_add=True)),
        migrations.AlterField(model_name='contestproblem', name='created_by', field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='created_contest_problems', to=settings.AUTH_USER_MODEL)),
        migrations.AlterField(model_name='contestproblem', name='updated_at', field=models.DateTimeField(auto_now=True)),
    ]
