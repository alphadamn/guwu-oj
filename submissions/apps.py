from django.apps import AppConfig


class SubmissionsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'submissions'

    def ready(self):
        # Real-time WebSocket push: publish on Submission / test-result saves.
        from .signals import connect_signals
        connect_signals()
