"""Free space on the drive that holds this hub's own ledger. Framework-free.

A self-hosted hub shares its disk with whatever else runs on the host: other services, their
databases, their pre-deploy snapshots and backups. When that disk fills, nothing announces it
as a disk problem -- appends start failing, snapshots are refused, deploys roll back -- and
every symptom reads as a different bug. One `disk_usage` call on the hub's own drive turns
that into a named condition while there is still room to act.

The reading is ALWAYS reported (a quiet drive is shown with its numbers, never as absence),
and the condition fires only below a threshold:

* HUB_DISK_WARN_GB      (default 15) -- below this, a warning on the attention rail
* HUB_DISK_CRITICAL_GB  (default 8)  -- below this, it ranks with the most urgent items

The reading is cached for TTL_S seconds: the board reads it on every snapshot, and a disk
does not change meaningfully faster than that. An unreadable drive reports `error` and fires
nothing -- a detector that cannot measure must say so, never guess "full" or "fine".
"""
from __future__ import annotations

import os
import platform
import shutil
import threading
import time
from pathlib import Path

TTL_S = 120
_GIB = 1024 ** 3
_CACHE: dict = {"key": None, "at": 0.0, "value": None}
_LOCK = threading.Lock()


def _gb_setting(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def thresholds() -> dict:
    warn = _gb_setting("HUB_DISK_WARN_GB", 15.0)
    critical = min(_gb_setting("HUB_DISK_CRITICAL_GB", 8.0), warn)
    return {"warn_gb": warn, "critical_gb": critical}


def _measure(hub_dir) -> dict:
    path = Path(hub_dir)
    # Walk up to a directory that exists: a brand-new hub may not have created HUB_DIR yet,
    # and the question is about the DRIVE, which its nearest existing ancestor sits on.
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(str(probe))
    except OSError as exc:
        return {"host": platform.node(), "path": str(path), "error": str(exc)[:200]}
    total = usage.total or 0
    return {
        "host": platform.node(),
        "path": str(path),
        "drive": probe.anchor or str(probe),
        "total_gb": round(total / _GIB, 2),
        "free_gb": round(usage.free / _GIB, 2),
        "free_pct": round(100.0 * usage.free / total, 1) if total else 0.0,
    }


def reading(hub_dir, now: float | None = None) -> dict:
    """The drive's size and free space plus the verdict against the thresholds:
    state is `ok`, `warn`, `critical`, or `unmeasured` (with `error`)."""
    now = time.time() if now is None else now
    limits = thresholds()
    key = (str(hub_dir), limits["warn_gb"], limits["critical_gb"])
    with _LOCK:
        if _CACHE["key"] == key and now - _CACHE["at"] < TTL_S and _CACHE["value"]:
            return dict(_CACHE["value"])
    value = _measure(hub_dir)
    value.update(limits)
    value["measured_at"] = now
    if value.get("error") or not value.get("total_gb"):
        value["state"] = "unmeasured"
    elif value["free_gb"] < limits["critical_gb"]:
        value["state"] = "critical"
    elif value["free_gb"] < limits["warn_gb"]:
        value["state"] = "warn"
    else:
        value["state"] = "ok"
    with _LOCK:
        _CACHE.update(key=key, at=now, value=dict(value))
    return value


def describe(value: dict) -> str:
    """One line a person can act on, naming the drive and its numbers."""
    return ("%s: %s has %.1f GB free of %.0f GB (%.1f%%) -- below the %s threshold of %.0f GB. "
            "Appends, snapshots and backups on this drive fail once it fills; free space "
            "before that, starting with what else the host keeps on it."
            % (value.get("host") or "this host", value.get("drive") or "the hub's drive",
               value.get("free_gb") or 0.0, value.get("total_gb") or 0.0,
               value.get("free_pct") or 0.0,
               "critical" if value.get("state") == "critical" else "warning",
               value.get("critical_gb") if value.get("state") == "critical"
               else value.get("warn_gb")))
