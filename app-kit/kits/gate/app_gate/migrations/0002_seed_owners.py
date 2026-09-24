"""Seed exactly this app's OWNERS as superadmin — nobody else.

A roster is not something a migration can know: a migration cannot tell who should open an app
it has never met. The one thing it can know is who the app's owners are, because settings say
so. Anything else — importing another table's rows, a shared legacy roster, a list of people
from the app this kit was cut from — would admit strangers as members of an app they have never
heard of, silently. People are added afterwards with ``manage.py roster``, deliberately and
audited.
"""
from django.conf import settings
from django.db import migrations


def seed(apps, schema_editor):
    AccessUser = apps.get_model("app_gate", "AccessUser")
    GateEvent = apps.get_model("app_gate", "GateEvent")
    owners = getattr(settings, "APP_GATE_SUPERADMINS", [])
    if isinstance(owners, str):
        raise ValueError("APP_GATE_SUPERADMINS must be a list (see app_gate.E001)")
    for raw in owners:
        name = str(raw).strip()
        if "\\" in name:
            name = name.rsplit("\\", 1)[1]
        name = name.split("@", 1)[0].casefold()
        if not name:
            continue
        _, created = AccessUser.objects.update_or_create(
            username=name, defaults={"role": "superadmin", "active": True})
        GateEvent.objects.create(actor="migration", action="roster-seed-owner",
                                 outcome="added" if created else "kept",
                                 detail={"username": name})


class Migration(migrations.Migration):
    dependencies = [("app_gate", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
