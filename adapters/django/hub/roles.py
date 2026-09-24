"""Process roles: which process answers requests and which one does the background work.

``HUB_ROLE`` (read per call, from the environment):

- unset / ``all`` -- today's single-process shape: the request process also runs the background
  projections (delivery ancestry) on daemon threads.
- ``web`` -- serves requests and starts NO background projection thread. It reads what the
  backgrounder published (a sidecar under ``HUB_DIR``). If the backgrounder's clock is older than
  ``HUB_BACKGROUND_STALE_S`` (default 180 s) or absent, the web process builds ONE projection
  itself, single-flight, so a dead backgrounder degrades to the single-process shape instead of
  freezing the board.
- ``background`` -- the ``manage.py hubbackground`` loop: folds the ledger on its own clock,
  materializes the projections into sidecars and publishes a realtime wake-up when one changes.

Why: git-ancestry measurement, folds and audits compete with request threads for the
interpreter; on a single process a burst of background work shows up as slow writes and slow
streams exactly when every client reconnects. Two processes from one checkout remove that
contention, and a rolling restart of the web processes (one at a time, each healthy before the
next) is what makes a deploy invisible -- see ``docs/OPERATIONS.md``.
"""
import json
import os
import time
from pathlib import Path

STARTED_AT = time.time()
_ROLES = ("all", "web", "background")


def role() -> str:
    value = (os.environ.get("HUB_ROLE") or "all").strip().lower()
    return value if value in _ROLES else "all"


def runs_background_here() -> bool:
    """True when THIS process may start background projection work on its own threads."""
    return role() != "web"


def stale_after_s() -> float:
    try:
        return float(os.environ.get("HUB_BACKGROUND_STALE_S") or 180)
    except ValueError:
        return 180.0


def clock_path(hub_dir) -> Path:
    return Path(hub_dir) / "background.json"


def write_clock(hub_dir, **fields) -> None:
    """The backgrounder's heartbeat: written after every tick, read by web processes."""
    from hub_core import atomic
    target = clock_path(hub_dir)
    tmp = target.with_name(target.name + ".%d.tmp" % os.getpid())
    tmp.write_text(json.dumps({"pid": os.getpid(), "tick_at": time.time(), **fields},
                              sort_keys=True), encoding="utf-8")
    atomic.replace(tmp, target)


def read_clock(hub_dir) -> dict:
    """The backgrounder's last tick and whether it is fresh. Never raises."""
    try:
        data = json.loads(clock_path(hub_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "absent"}
    age = max(0.0, time.time() - float(data.get("tick_at") or 0))
    data["age_s"] = round(age, 1)
    data["status"] = "fresh" if age <= stale_after_s() else "stale"
    return data


def backgrounder_fresh(hub_dir) -> bool:
    return read_clock(hub_dir).get("status") == "fresh"


def process_info(hub_dir) -> dict:
    """What perf.json reports about the ANSWERING process -- so a slow response can be traced to
    the process that served it, not to "the hub"."""
    return {"role": role(), "pid": os.getpid(),
            "started_at": int(STARTED_AT), "uptime_s": int(time.time() - STARTED_AT),
            "background_clock": read_clock(hub_dir),
            "stale_after_s": stale_after_s()}
