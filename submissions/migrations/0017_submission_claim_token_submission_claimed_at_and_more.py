# Phase 2: submission lifecycle state machine (claim / lease / fence).
#
# Adds judge_state + claim bookkeeping columns and backfills lifecycle state
# from the pre-existing verdict column. Rollback simply drops the columns; no
# historic verdict data lives in them, so no reverse data migration is needed.

from django.conf import settings
from django.db import migrations, models
from django.db.models import F


def backfill_judge_state(apps, schema_editor):
    """Infer lifecycle state from the pre-existing verdict column.

    Any non-Pending verdict is terminal by definition (including System
    Error), so those rows settle as DONE with finished_at approximated by
    created_at. Rows still Pending predate the claim machinery and stay
    PENDING; the reaper only reclaims QUEUED/JUDGING rows, so legacy
    abandoned Pending rows are never silently re-judged.
    """
    Submission = apps.get_model('submissions', 'Submission')
    Submission.objects.exclude(status='Pending').update(
        judge_state='DONE',
        finished_at=F('created_at'),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('contests', '0004_contest_entry_points_cost_contestenrollment'),
        ('problems', '0007_alter_problem_options_alter_solution_options_and_more'),
        ('submissions', '0016_judge_machine_transport_security'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='submission',
            name='claim_token',
            field=models.UUIDField(blank=True, null=True, unique=True),
        ),
        migrations.AddField(
            model_name='submission',
            name='claimed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='submission',
            name='finished_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='submission',
            name='heartbeat_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='submission',
            name='judge_state',
            field=models.CharField(
                choices=[
                    ('PENDING', 'Pending'),
                    ('QUEUED', 'Queued'),
                    ('JUDGING', 'Judging'),
                    ('DONE', 'Done'),
                    ('FAILED', 'Failed'),
                ],
                db_index=True, default='PENDING', max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='submission',
            name='worker_id',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.RunPython(backfill_judge_state, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name='submission',
            index=models.Index(
                fields=['judge_state', 'heartbeat_at'],
                name='subm_judgestate_hb_idx',
            ),
        ),
    ]
