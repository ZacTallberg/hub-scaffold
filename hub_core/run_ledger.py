"""The cross-machine run ledger: one row per unattended run, from every machine, in one place.

Every unattended launcher keeps a row per run in its OWN machine's ledger (``<home>/runs.json``):
the item, how long it took, its exit code, its tokens, its outcome. Nobody but that machine could
read it, so the runs of every other machine were not measurable at all -- "why are responders
slow?" and "which machine's runs keep failing?" had no data behind them. The launcher now posts
the row when a run ends (``POST /hub/api/run-ledger``) and the hub keeps it here, beside the
ledger, never in it: a measurement is not a board fact, and the fold every read pays for never
carries it.

Rows are compact append-only JSONL in daily files (``<hub_dir>/run-ledger/YYYYMMDD.jsonl``),
pruned past ``RETENTION_DAYS``. ``runs()`` answers a ROLLING window (``ts >= now - days``, never
"N calendar files", which is a partial day short), one row per run id (a launcher whose post
outlived its client timeout may resend it; the first copy stands), with per-machine and overall
summaries. The rows it returns are capped by a byte-sized backstop, and when the cap bites the
answer SAYS so with the span the rows really cover -- a "7-day" answer whose rows silently
covered three days is how a reader draws the wrong conclusion from a right number.

``phases`` (where a run's wall time went: waiting on a pipeline, tests, setup, hub calls,
building, verifying, orientation) is optional and only ever measured by the launcher from the
session's own record; a run without it was not measured, never zero.

Framework-free, stdlib only. Recording is fail-soft: measurement never breaks a launcher.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

RETENTION_DAYS = 120
ROWS_MAX = 5000
PHASES = ("pipeline_wait", "tests", "setup", "hub", "build", "verify", "orientation", "other")
_TEXT = {"item": 120, "item_kind": 20, "outcome": 40, "run_id": 80, "session": 64, "repo": 160,
         "runtime": 20, "lane": 20}
_INTS = ("duration_s", "tokens", "turns", "suspended_s")
_LAST_PRUNE = [0.0]


def _dir(hub_dir) -> Path:
    return Path(hub_dir) / "run-ledger"


def _daymark(ts: float) -> str:
    return time.strftime("%Y%m%d", time.gmtime(ts))


def _int(value):
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return None


def record(hub_dir, agent: str, machine: str, row: dict) -> dict:
    """Coerce one posted run row field by field and append it. Returns the row written, or {}
    when the body carried no item (nothing to record). A malformed value is dropped, never
    raised: this is a body from another machine."""
    if not isinstance(row, dict) or not str(row.get("item") or "").strip():
        return {}
    rec = {"ts": time.time(), "agent": str(agent or "").strip().lower()[:60],
           "machine": str(machine or row.get("machine") or "").strip().lower()[:60]}
    # A launcher naturally calls the ITEM's kind `kind`. Read it as item_kind, never as a field
    # of the row: where a ledger's own row type was also `kind`, the item's kind overwrote it
    # and every posted run was filed somewhere the reader never looked.
    if row.get("item_kind") in (None, "") and row.get("kind") not in (None, ""):
        row = dict(row, item_kind=row.get("kind"))
    for key, limit in _TEXT.items():
        if row.get(key) not in (None, ""):
            rec[key] = str(row.get(key))[:limit]
    for key in _INTS:
        value = _int(row.get(key))
        if value is not None:
            rec[key] = value
    rc = row.get("rc")
    rec["rc"] = rc if isinstance(rc, int) and not isinstance(rc, bool) else None
    try:
        started = float(row.get("started") or 0)
        rec["started"] = started if started > 0 else None
    except (TypeError, ValueError):
        rec["started"] = None
    phases = row.get("phases")
    if isinstance(phases, dict):
        clean = {p: round(float(phases[p]), 1) for p in PHASES
                 if isinstance(phases.get(p), (int, float)) and not isinstance(phases.get(p), bool)
                 and phases[p] >= 0}
        if clean:
            wall = phases.get("wall_s")
            clean["wall_s"] = (round(float(wall), 1) if isinstance(wall, (int, float))
                               and not isinstance(wall, bool) else round(sum(clean.values()), 1))
            rec["phases"] = clean
    folder = _dir(hub_dir)
    folder.mkdir(parents=True, exist_ok=True)
    if rec["ts"] - _LAST_PRUNE[0] > 86400:
        # Retention rides the writes (at most once a day per process): a hub that runs no
        # background role must still not keep every day it was ever told about.
        _LAST_PRUNE[0] = rec["ts"]
        prune(hub_dir, rec["ts"])
    with (folder / ("%s.jsonl" % _daymark(rec["ts"]))).open("a", encoding="utf-8",
                                                               newline="\n") as fh:
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return rec


def _iter_rows(hub_dir, days: int):
    folder = _dir(hub_dir)
    now = time.time()
    for d in range(days):
        path = folder / ("%s.jsonl" % _daymark(now - d * 86400))
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


def _count(values) -> dict:
    out: dict = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _summary(group: list) -> dict:
    durations = sorted(int(r.get("duration_s") or 0) for r in group)
    phased = [r for r in group if isinstance(r.get("phases"), dict)]
    totals = {p: sum(float(r["phases"].get(p) or 0) for r in phased) for p in PHASES}
    whole = sum(totals.values())
    return {"runs": len(group),
            "median_duration_s": durations[len(durations) // 2] if durations else None,
            "tokens": sum(int(r.get("tokens") or 0) for r in group),
            "outcomes": _count(r.get("outcome") or "?" for r in group),
            "phased_runs": len(phased),
            "phase_minutes": {p: round(totals[p] / 60, 1) for p in PHASES},
            "phase_share": ({p: round(100 * totals[p] / whole, 1) for p in PHASES}
                            if whole else None)}


def runs(hub_dir, days: int = 7, machine: str = "", item: str = "") -> dict:
    """The ledger across machines over a ROLLING ``days`` window: rows newest first (capped at
    ROWS_MAX with the cap said out loud), and per machine and overall the run count, median
    duration, tokens, outcomes and the SHARE of measured wall time per phase -- a share is
    computed only over runs that carried phases, and says how many that was."""
    days = max(1, min(int(days or 7), RETENTION_DAYS))
    machine, item = str(machine or "").strip().lower(), str(item or "").strip()
    since = time.time() - days * 86400
    rows, seen = [], set()
    for row in _iter_rows(hub_dir, days + 1):
        try:
            if float(row.get("ts") or 0) < since:
                continue
        except (TypeError, ValueError):
            continue
        if machine and row.get("machine") != machine:
            continue
        if item and not str(row.get("item") or "").endswith(item):
            continue
        rid = str(row.get("run_id") or "")
        if rid:
            if rid in seen:
                continue                     # a resent post: the first copy stands
            seen.add(rid)
        rows.append(row)
    rows.sort(key=lambda r: -(r.get("ts") or 0))
    by_machine: dict = {}
    for row in rows:
        by_machine.setdefault(row.get("machine") or "?", []).append(row)
    shown = rows[:ROWS_MAX]
    oldest = min((r.get("ts") or 0 for r in shown), default=0) or None
    return {"window_days": days, "count": len(rows), "overall": _summary(rows),
            "machines": {m: _summary(g) for m, g in sorted(by_machine.items())},
            "rows": shown, "rows_returned": len(shown),
            "rows_truncated": len(rows) > len(shown),
            "rows_oldest_ts": oldest,
            "rows_span_h": round((time.time() - oldest) / 3600, 1) if oldest else None,
            "note": "phases are measured by the launcher from the run's own record; a run "
                    "without them was not measured, not zero"}


def prune(hub_dir, now: float | None = None) -> int:
    """Drop daily files past RETENTION_DAYS. Returns the count removed. Never raises."""
    now = time.time() if now is None else now
    keep = {_daymark(now - d * 86400) for d in range(RETENTION_DAYS)}
    removed = 0
    try:
        for path in _dir(hub_dir).glob("*.jsonl"):
            if path.stem not in keep:
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
    except OSError:
        pass
    return removed
