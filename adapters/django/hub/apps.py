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
        from . import checks  # noqa: F401  (the fold/schema contract, run by `manage.py check`)
        # A thread (or the process) that dies of an exception lands in the hub's own error
        # stream, not only in the service's stderr. Fail-soft: arming must never stop a start.
        try:
            from hub_core import errorlog

            from . import hub_app
            errorlog.install_excepthooks(hub_app.HUB_DIR)
        except Exception:                                    # noqa: BLE001
            pass
