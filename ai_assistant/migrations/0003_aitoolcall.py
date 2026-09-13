from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('ai_assistant', '0002_aigeneration_reasoning'),
        ('submissions', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='AIToolCall',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True, primary_key=True, serialize=False, verbose_name='ID',
                )),
                ('seq', models.PositiveSmallIntegerField(verbose_name='第几次工具调用')),
                ('tool_name', models.CharField(
                    default='submit_to_judge', max_length=64, verbose_name='工具名称',
                )),
                ('language', models.CharField(
                    blank=True, default='', max_length=32, verbose_name='语言',
                )),
                ('code', models.TextField(blank=True, default='', verbose_name='提交的代码')),
                ('status', models.CharField(
                    choices=[
                        ('Pending', '评测中'),
                        ('Accepted', 'Accepted'),
                        ('Wrong Answer', 'Wrong Answer'),
                        ('Time Limit Exceeded', 'Time Limit Exceeded'),
                        ('Memory Limit Exceeded', 'Memory Limit Exceeded'),
                        ('Runtime Error', 'Runtime Error'),
                        ('Compile Error', 'Compile Error'),
                        ('System Error', 'System Error'),
                        ('Timeout', '等待判题超时'),
                        ('Invalid', '参数无效'),
                        ('Error', '调用失败'),
                    ],
                    default='Pending', max_length=30, verbose_name='评测结果',
                )),
                ('passed_cases', models.PositiveIntegerField(default=0, verbose_name='通过测试点数')),
                ('total_cases', models.PositiveIntegerField(default=0, verbose_name='总测试点数')),
                ('runtime_ms', models.PositiveIntegerField(
                    blank=True, null=True, verbose_name='最大耗时(ms)',
                )),
                ('memory_kb', models.PositiveIntegerField(
                    blank=True, null=True, verbose_name='最大内存(KB)',
                )),
                ('result_json', models.TextField(blank=True, default='', verbose_name='返回给模型的结果')),
                ('error_message', models.CharField(
                    blank=True, default='', max_length=300, verbose_name='错误信息',
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('generation', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='tool_calls', to='ai_assistant.aigeneration',
                )),
                ('submission', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='ai_tool_calls', to='submissions.submission',
                )),
            ],
            options={
                'verbose_name': 'AI 工具调用',
                'verbose_name_plural': 'AI 工具调用',
                'ordering': ['created_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='aitoolcall',
            constraint=models.UniqueConstraint(
                fields=('generation', 'seq'),
                name='uniq_ai_tool_seq_per_generation',
            ),
        ),
    ]
