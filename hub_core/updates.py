"""The agents' own first-person feed of what they DID — "see it in a chat when something is fixed".

When an agent closes a loop (answers an ask, acks an error, ships a fix) it posts one human-readable
line here, with evidence that dereferences. The activity rail already shows canonical ledger events
in the system's words, mixed with everything else; a person asking "is the autonomous layer earning
its keep" wants the narrative in the AGENT's words, separated from the machinery. This is that
narrative. Framework-free; every function takes the hub dir explicitly (the presence idiom).

A SIDECAR, like presence and the error stream: observed narrative, never ledger truth. Bounded
JSONL, newest first on read, folded into the board's live block so it streams over the same push
tick as everything else.

Rules this module holds:

* APPEND, NEVER READ-MODIFY-REPLACE ON THE HOT PATH. The board folds this file in on every live
  tick without a lock, so a reader holding the file open is the ordinary case. On Windows a replace
  onto a path another process has open fails outright, and retrying the replace is not enough — a
  reader that outlives the retry window still costs the row. An append cannot lose to a reader.
* TRIM IS FAIL-SOFT and runs after the row is durable. A trim that loses the race costs nothing
  but a temporarily longer file: read() caps its own output and the next record() trims again.
* A LOST WRITE SAYS WHY. record() returns ``(None, reason)`` instead of swallowing the cause, so
  the adapter can answer a retryable 503 naming it and record a warning row — an opaque 500 with an
  empty traceback is how a feed race stays undiagnosable forever.
* The tmp path is pid-unique so a stale-lock reclaim can never let two trimmers truncate each other.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .process_lock import ProcessFileLock

KEEP = 200                  # a recent narrative, not an archive
LOCK_TIMEOUT_S = 20         # a burst of agents reporting at once must queue, not drop
KINDS = ("fixed", "answered", "acked", "shipped", "escalated", "noop")
REPLACE_ATTEMPTS = 6


def _path(hub_dir) -> Path:
    return Path(hub_dir) / "agent-updates.jsonl"


def _replace(tmp: Path, dest: Path) -> None:
    """os.replace tolerating the Windows sharing window; the last attempt may raise."""
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            tmp.replace(dest)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(0.02 * (attempt + 1))


def _trim(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    if len(lines) <= KEEP:
        return
    tmp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    try:
        tmp.write_text("\n".join(lines[-KEEP:]) + "\n", encoding="utf-8")
        _replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def record(hub_dir, *, agent, kind, summary, machine="", evidence="", item="", by=""):
    """Append one update. Returns ``(row, None)`` or ``(None, reason)`` — never raises, because a
    feed write must not break the action it is narrating."""
    kind = kind if kind in KINDS else "fixed"
    row = {
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "epoch": time.time(),
        "agent": str(agent or "an agent")[:60],
        "machine": str(machine or "")[:60],
        "kind": kind,
        # Whole: a line a person writes has no other copy, and a cut evidence URL is a broken
        # pointer. The fields around these are names, not prose.
        "summary": str(summary or "").replace("\x00", " ")[:4000],
        "evidence": str(evidence or "")[:1000],
        "item": str(item or "")[:120],
        # "autoworker" when an unattended pass posted it, "human" when a person's verb did.
        "by": str(by or "")[:20],
    }
    where = "lock"
    try:
        path = _path(hub_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with ProcessFileLock(path.parent, name=".agent-updates.lock", timeout=LOCK_TIMEOUT_S):
            where = "append"
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            where = "trim"
            _trim(path)
    except Exception as exc:                                 # noqa: BLE001
        if where == "trim":
            return row, None            # the row is durable; a lost trim is cosmetic
        return None, "%s at %s" % (type(exc).__name__, where)
    return row, None


def read(hub_dir, limit=60) -> list:
    """Newest first, capped. Cheap enough to fold into every live tick; never raises."""
    try:
        path = _path(hub_dir)
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue                # a torn line is skipped, never fatal
            if isinstance(row, dict):
                rows.append(row)
        rows.reverse()
        return rows[:max(1, int(limit))]
    except Exception:                                        # noqa: BLE001
        return []


def stamp(hub_dir) -> tuple:
    """Cheap change fingerprint (size, mtime) so snapshot memos see a new line immediately."""
    try:
        st = _path(hub_dir).stat()
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return (0, 0)
