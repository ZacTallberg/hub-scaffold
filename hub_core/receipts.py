"""A notification that failed is a RECORDED FACT, not silence.

The hub addresses items to agents (hub_core.inbox) and retires them when they land (an ack, a
message ack). Without a lifecycle record, "nobody was told" and "told, and ignored it" look
identical afterwards, and the only evidence of a failed delivery is its absence. This module
writes down what happened at each stage the hub itself owns:

  offered    the hub handed this item to a caller asking what is addressed to them
  delivered  the recipient acknowledged it (an ack, a message ack)
  failed     an attempt was refused, and by what
  resolved   the condition behind it went away

It is deliberately NOT a transport: the existing channels deliver; this only records.

Two rules keep the log honest and small. Every row carries a FINGERPRINT of the condition
(kind, ref, agent), so one recurring thing is one thread. A repeat of the same
(fingerprint, stage, outcome) inside the dedup window increments a count on the existing row
instead of appending — a console polling its inbox every few seconds must not be able to flood
the record of what it was told. (The inbox additionally records an offer only when the
addressed SET changes.)

`undelivered()` answers the question the module exists for — offered and never acknowledged —
computed at read time from the rows, never stored as a flag that could go stale.

Framework-free; every function takes the hub dir explicitly. Never a dependency: every call is
fail-soft, a torn line is skipped, and an unknown stage is refused rather than stored.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from pathlib import Path

from .process_lock import ProcessFileLock

FILE_NAME = "receipts.jsonl"
MAX_ROWS = 4000             # evidence for the window in which somebody asks "was I told?"
DEDUP_WINDOW_S = 900
LOCK_TIMEOUT_S = 5
STAGES = ("offered", "delivered", "failed", "resolved")


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _path(hub_dir) -> Path:
    return Path(hub_dir) / FILE_NAME


def fingerprint(kind: str, ref: str, agent: str = "") -> str:
    """Identity of the CONDITION, so one recurring thing is one thread."""
    return hashlib.sha256(("%s|%s|%s" % (kind, ref, agent)).encode("utf-8", "replace")
                          ).hexdigest()[:16]


def _read(hub_dir) -> list:
    try:
        text = _path(hub_dir).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue                        # a torn line is skipped, never fatal
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _write(hub_dir, rows: list) -> None:
    p = _path(hub_dir)
    tmp = p.with_name("%s.%d.tmp" % (p.name, os.getpid()))
    tmp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows[-MAX_ROWS:]),
                   encoding="utf-8")
    os.replace(tmp, p)


def _age_s(when: str) -> float:
    try:
        stamp = _dt.datetime.strptime(when, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc)
        return (_dt.datetime.now(_dt.timezone.utc) - stamp).total_seconds()
    except (TypeError, ValueError):
        return 1e9


def record(hub_dir, kind: str, ref: str, stage: str, *, agent: str = "", outcome: str = "ok",
           detail: str = "") -> dict:
    """Write one receipt; returns the row (or the row it collapsed onto). Never raises."""
    try:
        if stage not in STAGES or not ref:
            return {}
        fp = fingerprint(kind, ref, agent)
        p = _path(hub_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        with ProcessFileLock(p.parent, name=".receipts.lock", timeout=LOCK_TIMEOUT_S):
            rows = _read(hub_dir)
            for row in reversed(rows):
                if (row.get("fingerprint") == fp and row.get("stage") == stage
                        and row.get("outcome") == outcome
                        and _age_s(str(row.get("last") or row.get("at") or "")) <= DEDUP_WINDOW_S):
                    row["count"] = int(row.get("count") or 1) + 1
                    row["last"] = _now()
                    _write(hub_dir, rows)
                    return row
            row = {"fingerprint": fp, "kind": str(kind)[:40], "ref": str(ref)[:160],
                   "stage": stage, "agent": str(agent)[:60], "outcome": str(outcome)[:60],
                   "detail": str(detail)[:300], "at": _now(), "last": _now(), "count": 1}
            rows.append(row)
            _write(hub_dir, rows)
            return row
    except Exception:                                        # noqa: BLE001 - never block delivery
        return {}


def for_ref(hub_dir, ref: str) -> list:
    """Every receipt about one item, oldest first: the thread of what happened to it."""
    return [r for r in _read(hub_dir) if r.get("ref") == ref]


def recent(hub_dir, limit: int = 50, *, stage: str = "", agent: str = "") -> list:
    """The newest receipts, optionally narrowed. Newest first, because that is the question."""
    rows = [r for r in _read(hub_dir)
            if (not stage or r.get("stage") == stage) and (not agent or r.get("agent") == agent)]
    return list(reversed(rows))[:max(1, int(limit))]


def undelivered(hub_dir, within_s: int = 86400) -> list:
    """Items OFFERED and never acknowledged (nor resolved), newest first — the ones nobody can
    prove landed. An informational item that is never acked by design (a crossover notice, a
    gate) appears here too; the caller narrows by kind when it only cares about ackable mail."""
    rows = _read(hub_dir)
    closed = {r.get("fingerprint") for r in rows if r.get("stage") in ("delivered", "resolved")}
    out = [r for r in rows
           if r.get("stage") == "offered" and r.get("fingerprint") not in closed
           and _age_s(str(r.get("at") or "")) <= within_s]
    return list(reversed(out))


def stamp(hub_dir) -> tuple:
    try:
        st = _path(hub_dir).stat()
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return (0, 0)
