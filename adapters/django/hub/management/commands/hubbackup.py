"""manage.py hubbackup -- a full, verified backup of the Hub, with an off-host copy.

    python manage.py hubbackup                       # take one (verified) into the vault
    python manage.py hubbackup --require-mirror      # ... and fail unless the off-host copy landed
    python manage.py hubbackup --status              # newest backup per vault; exit 1 when the
                                                     # newest OFF-HOST copy is older than 26 h
    python manage.py hubbackup --restore-into DIR    # extract the newest verified backup into an
                                                     # EMPTY directory (never over a live HUB_DIR)

WHAT IS CARRIED: the WHOLE ``HUB_DIR`` tree minus an exclude list (locks, temp files, the
rebuildable SQLite index). The polarity is deliberate: a hand-kept INCLUDE list can never contain
the file nobody added to it, and a backup missing a file is discovered on the day it is needed.
Forgetting to exclude something only makes the backup larger. The Django database is copied too
when it is SQLite (``VACUUM INTO`` on its own connection, then ``integrity_check`` and a per-table
row-count floor); another engine is recorded as such and belongs to that engine's own tooling.

WHAT "VERIFIED" MEANS: the live ledger's cursor (head seq, that event's hash, the file's size) is
recorded BEFORE bundling. The bundled events.jsonl must be at least that many bytes, and its event
at that seq must carry that hash -- a copy shorter than the source is refused, never compared only
as far as it reaches. The bundled ledger is then extracted into an isolated directory, folded, and
compared entity by entity with the live board at the recorded cursor (``hub_core.reconstruct``). An
empty fold, a comparison that examined nothing, or any differing entity refuses the backup; the
manifest records the cursor, both sizes, the board digest and how many entities were compared.

WHERE IT GOES: ``HUB_BACKUP_VAULT`` (setting or environment). The vault is PROVEN writable by
creating a probe file -- a parent directory that exists says nothing about whether this identity
may write there. If it is not writable the backup falls back to ``<BASE_DIR>/.hub-backups`` (keep
it out of version control) and says so. Retention is pruned BEFORE the copy, never after: a
prune that only runs after a successful write never runs again once the disk is full, and every
later backup then fails on the space the prune was meant to free. The fallback keeps fewer copies
than the vault, because it usually shares a disk with the thing it protects.

OFF-HOST COPY: ``HUB_BACKUP_MIRROR`` is either a directory (a mounted share or another disk) the
finished backup is copied into and re-hashed, or ``module:function`` -- an adapter seam called as
``function(folder: Path, manifest: dict) -> str`` that ships the backup anywhere else (object
storage, a database table) and returns where it put it. Only an off-host copy counts toward
``--status`` freshness: two copies on one disk are one copy.
"""
from __future__ import annotations

import fnmatch
import hashlib
import importlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

MARKER = "HUBBACKUP"
STALE_AFTER = timedelta(hours=26)          # a daily cadence, with room for drift
FALLBACK_KEEP = 3

# Rebuildable, or noise. Everything else under HUB_DIR rides along by default.
EXCLUDE_GLOBS = (
    "__pycache__", "*.pyc", "*.lock", "*.lock.*", ".*.lock", "*.tmp", "*.partial",
    "events.db", "events.db-wal", "events.db-shm",       # the index: rebuilt from events.jsonl
    # Console chat histories: what people typed to their assistants. A backup copy would outlive
    # HUB_HISTORY_RETENTION_DAYS and keep it long after the hub deleted it (docs/OPERATIONS.md).
    "histories",
)


def _setting(name, default=None):
    value = getattr(settings, name, None)
    return value if value not in (None, "") else (os.environ.get(name) or default)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _excluded(relpath: str) -> bool:
    return any(fnmatch.fnmatch(part, pattern)
               for part in relpath.split("/") for pattern in EXCLUDE_GLOBS)


def _writable(candidate: Path) -> bool:
    """Can THIS identity create files here? Probe it; never infer it from the path."""
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / (".write-probe-%d" % os.getpid())
        probe.write_bytes(b"x")
        probe.unlink()
        return True
    except OSError:
        return False


def vaults() -> list[tuple[Path, bool]]:
    """[(root, is_fallback)] in preference order -- the configured vault, then the fallback."""
    out = []
    configured = _setting("HUB_BACKUP_VAULT")
    if configured:
        out.append((Path(configured), False))
    out.append((Path(settings.BASE_DIR) / ".hub-backups", True))
    return out


def vault_choice() -> tuple[Path, bool, str]:
    configured = _setting("HUB_BACKUP_VAULT")
    for root, is_fallback in vaults():
        if _writable(root):
            why = "" if not is_fallback else (
                "%s is not writable by this identity" % configured if configured
                else "HUB_BACKUP_VAULT is not configured")
            return root, is_fallback, why
    raise CommandError("no writable backup vault (tried: %s)" % ", ".join(str(r) for r, _ in vaults()))


def backups(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted((p for p in root.iterdir() if p.is_dir() and (p / "manifest.json").exists()),
                  key=lambda p: p.name, reverse=True)


def read_manifest(folder: Path) -> dict:
    try:
        return json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _age_h(taken_at: str):
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(taken_at)).total_seconds() / 3600
    except (TypeError, ValueError):
        return None


def _prune(root: Path, keep: int, write) -> None:
    """Delete the oldest backups so that, AFTER the one about to be written, `keep` remain.
    Runs before the copy. Only folders this command wrote (they carry its manifest) are touched;
    half-written folders (no manifest) left by an interrupted run are removed too."""
    for folder in root.iterdir() if root.is_dir() else []:
        # An hour's grace, so a concurrent run's folder-in-progress is never mistaken for debris.
        if (folder.is_dir() and folder.name[:8].isdigit()
                and not (folder / "manifest.json").exists()
                and folder.stat().st_mtime < time.time() - 3600):
            shutil.rmtree(folder, ignore_errors=True)
            write("%s_PRUNED_PARTIAL %s" % (MARKER, folder.name))
    for folder in backups(root)[max(0, keep - 1):]:
        shutil.rmtree(folder, ignore_errors=True)
        write("%s_PRUNED %s" % (MARKER, folder.name))


def _copy_database(dest: Path) -> dict:
    if connection.vendor != "sqlite":
        return {"engine": connection.vendor, "copied": False,
                "why": "not SQLite -- back it up with the engine's own tooling"}
    source = str(connection.settings_dict["NAME"])
    if ":memory:" in source or "mode=memory" in source or not Path(source).exists():
        return {"engine": "sqlite", "copied": False, "why": "no on-disk database"}
    copy = dest / "db.sqlite3"
    reader = sqlite3.connect(source)           # its OWN connection: never block the live one
    try:
        # Counted BEFORE the copy: the copy must hold at least these rows (a floor), and rows
        # written while it runs may only add to it.
        tables = [r[0] for r in reader.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        source_counts = {t: reader.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
                         for t in tables}
        reader.execute("VACUUM INTO ?", (str(copy),))
    finally:
        reader.close()
    check = sqlite3.connect("file:%s?mode=ro" % copy, uri=True)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        copy_counts = {t: check.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
                       for t in source_counts}
    finally:
        check.close()
    if integrity != "ok":
        raise CommandError("copied database failed integrity_check: %s" % integrity)
    lost = [t for t in source_counts if copy_counts.get(t, -1) < source_counts[t]]
    if lost:
        raise CommandError("copied database lost rows in: %s" % ", ".join(lost[:5]))
    return {"engine": "sqlite", "copied": True, "integrity": integrity,
            "table_counts": copy_counts, "bytes": copy.stat().st_size, "sha256": _sha256(copy)}


def _bundle(hub_dir: Path, into: Path) -> list[str]:
    carried = []
    with zipfile.ZipFile(into, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(hub_dir.rglob("*")) if hub_dir.is_dir() else []:
            if not path.is_file():
                continue
            rel = path.relative_to(hub_dir).as_posix()
            if _excluded(rel):
                continue
            bundle.write(path, rel)
            carried.append(rel)
    return carried


def _mirror(folder: Path, manifest: dict) -> str:
    target = _setting("HUB_BACKUP_MIRROR")
    if not target:
        raise CommandError("HUB_BACKUP_MIRROR is not configured")
    if ":" in target and not os.path.isabs(target) and not Path(target).exists():
        module, _, func = target.partition(":")
        return str(getattr(importlib.import_module(module), func)(folder, manifest))
    dest = Path(target) / folder.name
    partial = dest.with_name(dest.name + ".partial")
    shutil.rmtree(partial, ignore_errors=True)
    shutil.copytree(folder, partial)
    for name in ("hub.zip", "db.sqlite3"):
        if (folder / name).exists() and _sha256(partial / name) != _sha256(folder / name):
            shutil.rmtree(partial, ignore_errors=True)
            raise CommandError("mirror copy of %s does not hash to the original" % name)
    partial.rename(dest)
    return str(dest)


def mirror_newest() -> tuple[str, float | None]:
    """Where the newest off-host copy is and how old it is (a directory mirror only)."""
    target = _setting("HUB_BACKUP_MIRROR")
    if not target:
        return "", None
    if ":" in target and not os.path.isabs(target) and not Path(target).exists():
        return target, None              # an adapter seam reports through the manifests
    found = backups(Path(target))
    if not found:
        return target, None
    return str(found[0]), _age_h(read_manifest(found[0]).get("taken_at", ""))


class Command(BaseCommand):
    help = "Take a full, verified backup of HUB_DIR (+ a SQLite database) with an off-host copy."

    def add_arguments(self, p):
        p.add_argument("--status", action="store_true")
        p.add_argument("--keep", type=int, default=7)
        p.add_argument("--require-mirror", action="store_true")
        p.add_argument("--reason", default="manual")
        p.add_argument("--restore-into", default="")

    def _say(self, line):
        self.stdout.write(line)
        self.stdout.flush()

    # ------------------------------------------------------------------ status
    def handle_status(self) -> int:
        newest_offhost = None
        for root, is_fallback in vaults():
            found = backups(root)
            label = "fallback vault" if is_fallback else "vault"
            if not found:
                self._say("%s: %s -- none" % (label, root))
                continue
            m = read_manifest(found[0])
            age = _age_h(m.get("taken_at", ""))
            self._say("%s: %s -- %d kept, newest %s (%.1fh old) entities %s digest %s mirror %s"
                      % (label, root, len(found), found[0].name, age or -1,
                         (m.get("reconstruct") or {}).get("compared"),
                         str((m.get("reconstruct") or {}).get("rebuilt_digest", ""))[:12],
                         m.get("mirror") or "NONE"))
            mirrored = [x for x in (_age_h(read_manifest(f).get("taken_at", "")) for f in found
                                    if read_manifest(f).get("mirror")) if x is not None]
            if mirrored:
                newest_offhost = min(mirrored + ([newest_offhost] if newest_offhost is not None else []))
        where, mirror_age = mirror_newest()
        if mirror_age is not None:
            self._say("mirror: %s (%.1fh old)" % (where, mirror_age))
            newest_offhost = mirror_age if newest_offhost is None else min(newest_offhost, mirror_age)
        elif not where:
            self._say("mirror: not configured (HUB_BACKUP_MIRROR) -- every copy is on this host")
        if newest_offhost is None or newest_offhost > STALE_AFTER.total_seconds() / 3600:
            self._say("%s_STALE no off-host copy newer than %dh" % (
                MARKER, STALE_AFTER.total_seconds() // 3600))
            return 1
        self._say("%s_FRESH newest off-host copy %.1fh old" % (MARKER, newest_offhost))
        return 0

    # ----------------------------------------------------------------- restore
    def handle_restore(self, target: Path) -> None:
        if target.exists() and any(target.iterdir()):
            raise CommandError("refusing to restore into a non-empty directory: %s" % target)
        from hub import hub_app
        if target.resolve() == Path(hub_app.HUB_DIR).resolve():
            raise CommandError("refusing to restore over the live HUB_DIR")
        candidates = [(read_manifest(f).get("taken_at", ""), f)
                      for root, _ in vaults() for f in backups(root)
                      if (read_manifest(f).get("reconstruct") or {}).get("ok")]
        if not candidates:
            raise CommandError("no verified backup in any vault")
        taken, folder = max(candidates)          # newest across EVERY vault, not the first one
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(folder / "hub.zip") as bundle:
            bundle.extractall(target)
        self._say("%s_RESTORED from=%s taken_at=%s into=%s -- point HUB_DIR at it after review"
                  % (MARKER, folder, taken, target))

    # ------------------------------------------------------------------ backup
    def handle(self, *a, status=False, keep=7, require_mirror=False, reason="manual",
               restore_into="", **o):
        if status:
            code = self.handle_status()
            if code:
                raise SystemExit(code)
            return
        if restore_into:
            return self.handle_restore(Path(restore_into))

        from hub import hub_app
        from hub_core import reconstruct

        hub_dir = Path(hub_app.HUB_DIR)
        if not (hub_dir / "events.jsonl").is_file():
            raise CommandError("no ledger at %s" % (hub_dir / "events.jsonl"))
        root, is_fallback, why = vault_choice()
        if is_fallback:
            self._say("%s_VAULT_FALLBACK root=%s reason=%s" % (MARKER, root, why))
            keep = min(keep, FALLBACK_KEEP)
        _prune(root, max(1, keep), self._say)            # BEFORE the copy
        free = shutil.disk_usage(root).free
        if free < 2 * sum(p.stat().st_size for p in hub_dir.rglob("*") if p.is_file()):
            raise CommandError("not enough free space in %s (%d bytes free)" % (root, free))

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = root / stamp
        n = 1
        while dest.exists():
            dest, n = root / ("%s.%d" % (stamp, n)), n + 1
        dest.mkdir(parents=True)
        try:
            database = _copy_database(dest)
            # The live cursor (head seq, its hash, the file's size) is recorded BEFORE bundling:
            # the copy must reach at least this far. Anything compared only as far as the copy
            # itself reaches would pass a truncated ledger as identical.
            cursor = reconstruct.live_cursor(hub_dir)
            carried = _bundle(hub_dir, dest / "hub.zip")
            if "events.jsonl" not in carried:
                raise CommandError("the bundle does not carry events.jsonl")
            with zipfile.ZipFile(dest / "hub.zip") as bundle:
                bundled_bytes = bundle.getinfo("events.jsonl").file_size
            if bundled_bytes < cursor["bytes"]:
                raise CommandError("backup refused: copy shorter than source -- the bundled "
                                   "events.jsonl is %d bytes, the live file was %d before bundling"
                                   % (bundled_bytes, cursor["bytes"]))
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as scratch:
                with zipfile.ZipFile(dest / "hub.zip") as bundle:
                    bundle.extract("events.jsonl", scratch)
                report = reconstruct.verify(hub_dir, scratch, cursor=cursor)
            if not report["ok"]:
                raise CommandError("backup refused: %s" % report["why"])
            manifest = {
                "taken_at": datetime.now(timezone.utc).isoformat(), "reason": reason,
                "build": hub_app._running_sha(), "hub_files": carried,
                "hub_zip_sha256": _sha256(dest / "hub.zip"),
                "hub_zip_bytes": (dest / "hub.zip").stat().st_size,
                "database": database,
                "reconstruct": {k: report[k] for k in ("ok", "compared", "head_seq", "head_hash",
                                                       "copy_head_seq", "rebuilt_digest",
                                                       "live_entities")},
                "ledger": {"live_bytes_before": cursor["bytes"], "bundled_bytes": bundled_bytes},
                "vault": str(root), "fallback": is_fallback, "mirror": "",
            }
            (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)       # never leave a half-written backup
            raise
        self._say("%s_TAKEN %s files=%d entities=%d digest=%s db=%s" % (
            MARKER, dest, len(carried), report["compared"], report["rebuilt_digest"][:12],
            "copied" if database.get("copied") else database.get("why")))

        try:
            where = _mirror(dest, manifest)
            manifest["mirror"] = where
            (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            if not callable_seam():
                shutil.copyfile(dest / "manifest.json", Path(where) / "manifest.json")
            self._say("%s_MIRRORED %s" % (MARKER, where))
        except Exception as exc:                            # noqa: BLE001
            self._say("%s_MIRROR_FAILED %s: %s" % (MARKER, type(exc).__name__, str(exc)[:200]))
            if require_mirror:
                # A run that cannot reach its off-host copy must not exit 0: a green job that
                # reports a backup protecting nothing is worse than a red one.
                raise CommandError("off-host copy did not land (--require-mirror)") from exc


def callable_seam() -> bool:
    target = _setting("HUB_BACKUP_MIRROR") or ""
    return ":" in target and not os.path.isabs(target) and not Path(target).exists()
