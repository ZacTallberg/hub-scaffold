# The table names come from the model module's constants, never literals: a literal here and a
# derived name in the model means migrate creates one table while every query asks for another.
from django.db import migrations, models

from app_gate.models import ACCESS_USER_TABLE, GATE_EVENT_TABLE


class Migration(migrations.Migration):
    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name="AccessUser",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False,
                                           verbose_name="ID")),
                ("username", models.CharField(max_length=128, unique=True)),
                ("role", models.CharField(choices=[
                    ("superadmin", "Super admin"), ("admin", "Admin"),
                    ("contributor", "Contributor"), ("member", "Member"),
                    ("visitor", "Visitor")], default="member", max_length=16)),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"db_table": ACCESS_USER_TABLE, "ordering": ("username",)},
        ),
        migrations.CreateModel(
            name="GateEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False,
                                           verbose_name="ID")),
                ("at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("actor", models.CharField(max_length=128)),
                ("action", models.CharField(max_length=48)),
                ("outcome", models.CharField(max_length=24)),
                ("detail", models.JSONField(blank=True, default=dict)),
            ],
            options={"db_table": GATE_EVENT_TABLE, "ordering": ("-at",)},
        ),
    ]
