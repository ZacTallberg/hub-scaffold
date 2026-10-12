"""When a project was deploying, so a browser's gateway failure INSIDE a restart is held, not queued.

A deploy restarts the service, and for those seconds the front door answers 502/503/504 or
nothing at all. Every open page that polls in that window forwards a failure through the
browser reporter, and on the instance this was lifted from each deploy reopened the same
"browser 503" problems on the queue -- a person was asked to look at a restart that had already
finished. The fix is not to hide gateway failures: one OUTSIDE a deploy is still a real outage.

So the CI ingest records each deploy job's window here (started -> finished; a running job is an
open window, capped), and the read-time bar asks one question of a browser gateway row: does its
WHOLE failure interval -- from the first failed attempt the row names ("over Ns") to its latest
occurrence -- lie inside the union of the serving project's deploy windows, each extended
``GRACE_S`` past its end? Then it is held, annotated with the window. A failure that began
before the deploy, outlasts the last one's end plus the grace, or has no overlapping deploy at
all is on the bar exactly as before. Any doubt (no windows, unreadable file) answers "not held".

Measured grace on the origin instance: the last browser rows of a restart landed 217-268 s after
the deploy job finished (pages back off before retrying), so the grace is 360 s.

Standard library only; the store is a small JSON sidecar in the hub dir, bounded per project.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .process_lock import ProcessFileLock

FILE = "deploy-windows.json"
GRACE_S = 360.0
#: A deploy job reported running and never finished is an open window for at most this long.
OPEN_CAP_S = 40 * 60
KEEP_S = 48 * 3600
KEEP_PER_PROJECT = 200

_GATEWAY = re.compile(r"\b(?:HTTP\s*)?(?:502|503|504)\b|\bno response\b|Failed to fetch|"
                      r"NetworkError|Load failed|\bHTTP 0\b", re.I)
_OVER = re.compile(r"\bover (\d+(?:\.\d+)?)\s*s\b", re.I)


def _path(hub_dir) -> Path:
    return Path(hub_dir) / FILE


_MEMO: dict = {}


def read(hub_dir) -> dict:
    """Every project's windows; re-read only when the file moves (the bar asks per row)."""
    path = _path(hub_dir)
    try:
        st = path.stat()
    except OSError:
        return {}
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _MEMO.get("key") == key:
        return _MEMO["value"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
    _MEMO.update(key=key, value=data)
    return data


def record(hub_dir, project: str, start: float, end: float | None, label: str = "",
           now: float | None = None) -> None:
    """Record (or extend) one deploy window for ``project``. ``end`` None = still running.
    Idempotent per label: a later delivery for the same job replaces its window. Never raises."""
    project = str(project or "").strip().lower()
    if not project or not start:
        return
    now = time.time() if now is None else now
    try:
        hub_dir = Path(hub_dir)
        hub_dir.mkdir(parents=True, exist_ok=True)
        with ProcessFileLock(hub_dir, name=".deploy-windows.lock", timeout=3):
            data = dict(read(hub_dir))
            rows = [w for w in data.get(project) or [] if isinstance(w, list) and len(w) == 3
                    and (not label or w[2] != label) and float(w[1] or w[0]) > now - KEEP_S]
            rows.append([float(start), float(end) if end else None, str(label or "")[:120]])
            rows.sort(key=lambda w: w[0])
            data[project] = rows[-KEEP_PER_PROJECT:]
            from . import atomic
            atomic.write_json(_path(hub_dir), data)
    except Exception:                                        # noqa: BLE001 - a hint, never a failure
        pass


def _ts(row) -> float:
    import datetime as _dt
    try:
        return _dt.datetime.fromisoformat(str(row.get("ts") or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def is_gateway_row(row) -> bool:
    """A browser-forwarded gateway failure: a 502/503/504 or a request that got no response."""
    src = str((row or {}).get("source") or "").lower()
    if not (src.startswith("browser.") or (src.startswith("app.") and src.endswith(".browser"))):
        return False
    return bool(_GATEWAY.search(str(row.get("message") or "")))


def held(row, windows: dict, projects, now: float | None = None) -> str:
    """The reason a gateway row is held (it lies wholly inside a deploy window + grace of one of
    ``projects``), else ''. ``windows`` is ``read(hub_dir)``."""
    if not is_gateway_row(row):
        return ""
    last = _ts(row)
    if not last:
        return ""
    m = _OVER.search(str(row.get("message") or ""))
    first = last - (float(m.group(1)) if m else 0.0)
    now = time.time() if now is None else now
    spans = []
    for project in projects or ():
        for start, end, label in windows.get(str(project).lower()) or []:
            stop = float(end) if end else min(now, float(start) + OPEN_CAP_S)
            spans.append((float(start), stop + GRACE_S, label))
    if not spans:
        return ""
    # Merge the windows, then ask whether ONE merged span covers the whole interval.
    spans.sort()
    merged = []
    for s, e, label in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e), merged[-1][2] + [label])
        else:
            merged.append((s, e, [label]))
    for s, e, labels in merged:
        if s <= first and last <= e:
            names = ", ".join(sorted({x for x in labels if x}))[:200]
            return "during deploy%s: the failure lies inside a restart" % (
                (" (%s)" % names) if names else "")
    return ""


def stamp(hub_dir) -> tuple:
    """Changes whenever a window is recorded: a cache of the bar's verdicts keys on it."""
    try:
        st = _path(hub_dir).stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


def default_serving(row) -> set:
    """The app a row came from: its context app, else the slug in an ``app.<slug>.*`` source."""
    ctx = (row or {}).get("context") or {}
    app = str(ctx.get("app") or (row or {}).get("origin_app") or "").strip().lower()
    src = str((row or {}).get("source") or "")
    if not app and src.startswith("app.") and src.count(".") >= 2:
        app = src.split(".")[1].lower()
    return {app} if app else set()


def bar(hub_dir, base, serving=None):
    """``base`` (a ``(on_bar, reason)`` predicate) with deploy-window holds applied: a gateway row
    wholly inside a deploy of a project ``serving(row)`` names is held with the window named.
    Any doubt keeps the base verdict."""
    serving = serving or default_serving
    windows = read(hub_dir)

    def predicate(row):
        on, why = base(row)
        if not on or not windows or not is_gateway_row(row):
            return on, why
        try:
            reason = held(row, windows, serving(row))
        except Exception:                                    # noqa: BLE001 - doubt queues
            reason = ""
        return (False, reason) if reason else (on, why)
    return predicate
