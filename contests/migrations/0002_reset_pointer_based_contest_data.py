from django.conf import settings
from django.db import migrations


def reset_pointer_based_contest_data(apps, schema_editor):
    """Delete the explicitly approved legacy pointer-based contest data.

    This migration is intentionally separate from the schema migration so
    PostgreSQL can commit foreign-key trigger events before the old pointer
    column is altered or removed. Raw deletes avoid model-state/schema drift
    in a previously interrupted migration attempt.
    """
    quote = schema_editor.quote_name
    schema_editor.execute(
        f'DELETE FROM {quote("submissions_submissiontestresult")} '
        f'WHERE submission_id IN ('
        f'SELECT id FROM {quote("submissions_submission")} '
        f'WHERE contest_problem_id IS NOT NULL'
        f')'
    )
    schema_editor.execute(
        f'DELETE FROM {quote("submissions_submission")} '
        f'WHERE contest_problem_id IS NOT NULL'
    )
    schema_editor.execute(f'DELETE FROM {quote("contests_contestproblem")}')


class Migration(migrations.Migration):
    dependencies = [
        ('contests', '0001_initial'),
        ('submissions', '0010_submission_contest_problem'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(reset_pointer_based_contest_data, migrations.RunPython.noop),
    ]
