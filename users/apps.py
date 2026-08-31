from django.apps import AppConfig


class UsersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'users'

    def ready(self):
        # Wire in the captcha-protected, 2FA-aware admin login view. Doing
        # this here (rather than at import time of ``urls``) means the
        # override applies even when ``admin.site.urls`` is referenced from
        # third-party apps. The import is local to avoid touching the app
        # registry during initial AppConfig loading.
        try:
            from oj_project.admin_site import patch_admin_login
            patch_admin_login()
        except Exception:
            # Never let the 2FA wiring prevent Django from booting —
            # the failure is loud enough in logs without bricking the
            # entire app registry.
            import logging
            logging.getLogger(__name__).exception(
                'Failed to patch admin login with captcha + 2FA'
            )
