from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _sqlite_pragmas(sender, connection, **kwargs):
    if connection.vendor == "sqlite":
        cur = connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA busy_timeout=20000;")


class HubConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "hub"
    verbose_name = "Project Hub"

    def ready(self):
        connection_created.connect(_sqlite_pragmas)
