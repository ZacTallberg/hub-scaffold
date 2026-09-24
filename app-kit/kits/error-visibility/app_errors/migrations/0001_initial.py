import django.utils.timezone
from django.db import migrations, models

from app_errors.models import APP_ERROR_TABLE, _IDX


class Migration(migrations.Migration):

    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name="AppError",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False,
                                           verbose_name="ID")),
                ("fingerprint", models.CharField(max_length=64, unique=True)),
                ("kind", models.CharField(choices=[
                    ("js", "Uncaught JavaScript error"), ("promise", "Unhandled promise rejection"),
                    ("http", "Failed request"), ("stream", "Live stream error"),
                    ("server", "Server exception"), ("background", "Background job failure"),
                    ("django", "Framework error"), ("data", "Data fault"),
                    ("other", "Logged error"), ("agent", "Agentic chat fault")],
                    default="js", max_length=12)),
                ("message", models.TextField()),
                ("stack", models.TextField(blank=True, default="")),
                ("source", models.CharField(blank=True, default="", max_length=500)),
                ("page_url", models.CharField(blank=True, default="", max_length=500)),
                ("status", models.IntegerField(blank=True, null=True)),
                ("actor", models.CharField(blank=True, default="", max_length=150)),
                ("user_agent", models.CharField(blank=True, default="", max_length=300)),
                ("count", models.PositiveIntegerField(default=1)),
                ("first_seen", models.DateTimeField(default=django.utils.timezone.now)),
                ("last_seen", models.DateTimeField(default=django.utils.timezone.now)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("resolved_by", models.CharField(blank=True, default="", max_length=150)),
            ],
            options={
                "db_table": APP_ERROR_TABLE,
                "ordering": ["-last_seen"],
                "indexes": [
                    models.Index(fields=["-last_seen"], name=f"{_IDX}_last_idx"),
                    models.Index(fields=["kind", "-last_seen"], name=f"{_IDX}_kind_idx"),
                    models.Index(fields=["resolved_at"], name=f"{_IDX}_res_idx"),
                ],
            },
        ),
    ]
