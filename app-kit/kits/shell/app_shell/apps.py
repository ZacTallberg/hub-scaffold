from django.apps import AppConfig


class AppShellConfig(AppConfig):
    name = "app_shell"
    label = "app_shell"
    verbose_name = "App shell"

    def ready(self):
        from . import checks  # noqa: F401
