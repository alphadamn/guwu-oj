import gzip

from django.db import migrations


def decompress_avatar_blobs(apps, schema_editor):
    AvatarBlob = apps.get_model('users', 'AvatarBlob')
    for avatar in AvatarBlob.objects.iterator(chunk_size=200):
        data = bytes(avatar.data or b'')
        if data.startswith(b'\x1f\x8b'):
            try:
                avatar.data = gzip.decompress(data)
            except OSError:
                continue
            avatar.save(update_fields=['data'])


class Migration(migrations.Migration):
    dependencies = [
        ('users', '0008_alter_user_options'),
    ]

    operations = [
        migrations.RunPython(decompress_avatar_blobs, migrations.RunPython.noop),
    ]
