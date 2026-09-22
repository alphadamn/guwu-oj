from django.apps import AppConfig


class ProblemsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'problems'

    def ready(self):
        # Keeps the judge-side test data cache key in step with edits to
        # test cases (see problems/fingerprint.py).
        from .fingerprint import connect_signals

        connect_signals()
