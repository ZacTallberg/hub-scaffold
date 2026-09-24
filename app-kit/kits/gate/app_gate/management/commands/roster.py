"""roster — read and change THIS app's gate roster from the command line.

    manage.py roster                                 # the active rows
    manage.py roster --add alice --role admin        # add, or reactivate and re-role
    manage.py roster --deactivate bob                # revoke; live on the next request
    manage.py roster --sync                          # active roster := APP_GATE_ALLOWLIST
    manage.py roster --all                           # include deactivated rows

The roster belongs to the running system, not to a schema change, so this is an operator's
deliberate act — never a migration, never automatic — and every change is appended to the
gate's event log.

FAIL-CLOSED, three ways:

* ``--sync`` refuses an empty allowlist: syncing to nothing would deactivate every row, and the
  allowlist is NOT a fallback once any row exists (deactivated rows still count as rows). The
  app would be unopenable short of editing the database.
* ``--sync`` and ``--deactivate`` refuse to leave the app without an owner.
* Nothing is ever deleted.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from app_gate.auth import normalize_username, owners
from app_gate.models import AccessUser, GateEvent

ROLES = tuple(AccessUser.ROLE_ORDER)


def _event(action: str, outcome: str, **detail) -> None:
    GateEvent.objects.create(actor="roster-command", action=action, outcome=outcome,
                             detail=detail)


class Command(BaseCommand):
    help = "List, add, deactivate or sync this app's gate roster (never deletes)."

    def add_arguments(self, parser):
        parser.add_argument("--add", metavar="USER")
        parser.add_argument("--role", choices=ROLES, default="member")
        parser.add_argument("--deactivate", metavar="USER")
        parser.add_argument("--sync", action="store_true")
        parser.add_argument("--all", action="store_true")

    def handle(self, *args, **opts):
        if opts["add"]:
            self._add(normalize_username(opts["add"]), opts["role"])
        if opts["deactivate"]:
            self._deactivate(normalize_username(opts["deactivate"]))
        if opts["sync"]:
            self._sync()
        self._list(opts["all"])

    def _add(self, name: str, role: str) -> None:
        if not name:
            raise CommandError("--add needs a username")
        row, created = AccessUser.objects.update_or_create(
            username=name, defaults={"role": role, "active": True})
        _event("roster-add", "added" if created else "updated", username=name, role=role)
        self.stdout.write(f"{'added' if created else 'updated'} {name} role={row.role}")

    def _deactivate(self, name: str) -> None:
        if name in owners():
            raise CommandError(f"{name} is an owner (APP_GATE_SUPERADMINS); owners are admitted "
                               "by name and cannot be revoked from the roster")
        changed = AccessUser.objects.filter(username=name, active=True).update(active=False)
        if not changed:
            raise CommandError(f"{name} is not an active roster member")
        _event("roster-deactivate", "deactivated", username=name)
        self.stdout.write(f"deactivated {name} (revoked on the next request)")

    def _sync(self) -> None:
        wanted = {normalize_username(v) for v in getattr(settings, "APP_GATE_ALLOWLIST", [])}
        wanted.discard("*")
        wanted.discard("")
        if not wanted:
            raise CommandError(
                "APP_GATE_ALLOWLIST is unset, empty, or only a wildcard. Refusing to sync: it "
                "would deactivate every row, and the allowlist is not a fallback once any row "
                "exists.")
        owner_set = set(owners())
        wanted |= owner_set
        if not owner_set:
            raise CommandError("APP_GATE_SUPERADMINS names nobody; refusing a sync that would "
                               "leave the app without an owner")
        for name in sorted(wanted):
            role = "superadmin" if name in owner_set else "member"
            current = AccessUser.objects.filter(username=name).first()
            if current and current.active and name not in owner_set:
                role = current.role            # a sync admits; it never demotes a member
            _, created = AccessUser.objects.update_or_create(
                username=name, defaults={"role": role, "active": True})
            _event("roster-sync", "added" if created else "kept", username=name, role=role)
            self.stdout.write(f"{'added' if created else 'kept'} {name} role={role}")
        for row in AccessUser.objects.filter(active=True).exclude(username__in=wanted):
            row.active = False
            row.save(update_fields=["active", "updated_at"])
            _event("roster-sync", "deactivated", username=row.username)
            self.stdout.write(f"deactivated {row.username} (not in APP_GATE_ALLOWLIST)")

    def _list(self, show_all: bool) -> None:
        rows = list(AccessUser.objects.all() if show_all
                    else AccessUser.objects.filter(active=True))
        active = sum(1 for r in rows if r.active)
        total = AccessUser.objects.count()
        self.stdout.write(f"roster: {active} active of {total} row(s)")
        owner_set = set(owners())
        for row in sorted(rows, key=lambda r: (not r.active, r.username)):
            tags = [t for t in ("owner" if row.username in owner_set else "",
                                "" if row.active else "deactivated") if t]
            self.stdout.write(f"  {row.username:<28} {row.role:<12} {' '.join(tags)}")
