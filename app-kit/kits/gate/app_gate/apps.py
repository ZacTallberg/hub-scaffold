from django.apps import AppConfig


class AppGateConfig(AppConfig):
    name = "app_gate"
    label = "app_gate"
    verbose_name = "Identity gate"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401  (registers the system checks)
