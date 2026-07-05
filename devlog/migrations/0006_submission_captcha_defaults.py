"""Apply new defaults for submission-captcha fields.

Previous migration ``0005`` added the three fields with Django defaults
of ``30 / 60`` (after this fix) — but the already-existing singleton row
keeps the old `10 / 5` values from the previous model default.
Set them explicitly so the admin page reflects the new defaults
immediately without the admin having to manually edit them.
"""
from django.db import migrations


def set_new_defaults(apps, schema_editor):
    CaptchaConfig = apps.get_model('devlog', 'CaptchaConfig')
    try:
        cfg = CaptchaConfig.objects.filter(pk=1).first()
    except Exception:
        cfg = None
    if cfg is None:
        return
    changed = False
    try:
        if getattr(cfg, 'captcha_submission_limit', None) in (None, 10):
            cfg.captcha_submission_limit = 30
            changed = True
    except Exception:
        pass
    try:
        if getattr(cfg, 'captcha_submission_window_minutes', None) in (None, 5):
            cfg.captcha_submission_window_minutes = 60
            changed = True
    except Exception:
        pass
    try:
        if getattr(cfg, 'captcha_submission_captcha_enabled', None) in (None,):
            cfg.captcha_submission_captcha_enabled = True
            changed = True
    except Exception:
        pass
    if changed:
        cfg.save()


def noop(apps, schema_editor):
    return None


class Migration(migrations.Migration):

    dependencies = [
        ('devlog', '0005_captchaconfig_captcha_submission_captcha_enabled_and_more'),
    ]

    operations = [
        migrations.RunPython(set_new_defaults, noop),
    ]
