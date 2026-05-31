# Generated manually for unique email constraint

from collections import defaultdict

from django.db import migrations, models


def dedupe_emails(apps, schema_editor):
    """Keep the oldest account per email; rename duplicates so unique index can apply."""
    User = apps.get_model('users', 'User')
    by_email = defaultdict(list)
    for user in User.objects.all().order_by('id'):
        key = (user.email or '').strip().lower()
        by_email[key].append(user)

    for users in by_email.values():
        if len(users) <= 1:
            continue
        for user in users[1:]:
            user.email = f'duplicate-{user.pk}@migration.local'
            user.save(update_fields=['email'])


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0003_merge_20260528_1624'),
    ]

    operations = [
        migrations.RunPython(dedupe_emails, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='user',
            name='email',
            field=models.EmailField(
                blank=True,
                max_length=254,
                unique=True,
                verbose_name='email address',
            ),
        ),
    ]
