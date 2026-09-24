from django.apps import AppConfig


class AppErrorsConfig(AppConfig):
    name = "app_errors"
    label = "app_errors"
    verbose_name = "Error visibility"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Arms every producer and sends the forwarder's arming row. Idempotent and
        # fail-soft: an installer that fails is reported, never raised into boot.
        from . import capture
        capture.install()
