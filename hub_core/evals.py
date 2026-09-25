"""Standing evals: one append-only trend per suite, measured on real data.

A retrieval, prompt or detector change "holds the line" only if the number that measures it is
kept somewhere a person can watch it move. `manage.py retrieval_eval` scores knowledge retrieval
on the board's OWN answered asks (nothing authored for an evaluation); each run can be recorded
here, and `/hub/eval.json` returns the series, so a regression is a visible step, not a feeling.

Stored in HUB_DIR/evals.jsonl, NEVER the ledger: a measurement is a reading, not a fact about
the project, and the ledger's fold is what every read path pays for. Writers are serialized by a
cross-process file lock, so the route and a management command on the same host cannot lose
each other's rows. Stdlib only.

A run is {suite, at, pairs, excluded, paths: {<path>: {n, "@1": pct, "@5": pct, ...}}, notes};
`paths` names each retrieval path or configuration the run scored.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import atomic
from .process_lock import ProcessFileLock

FILE_NAME = "evals.jsonl"
#: The trend keeps this many runs per suite; older ones are dropped oldest first.
KEEP_PER_SUITE = 400
MAX_PATHS = 32
SUITE_RX = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")


class EvalRefused(ValueError):
    def __init__(self, code: str, msg: str = ""):
        super().__init__(msg or code)
        self.code = code


def _path(hub_dir) -> Path:
    return Path(hub_dir) / FILE_NAME


def read(hub_dir) -> list[dict]:
    p = _path(hub_dir)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _count(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _clean_cells(cells: dict) -> dict:
    """One path's figures: numbers only (a percentage, a count, a latency), keys bounded."""
    out = {}
    for key, val in cells.items():
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            continue
        out[str(key)[:32]] = round(float(val), 3) if isinstance(val, float) else val
    return out


def normalize(run: dict, *, by: str = "", machine: str = "") -> dict:
    """The stored row, or EvalRefused. Refuses rather than clamps: a malformed run from a
    measuring tool is a bug in that tool, and quietly storing half of it hides the bug."""
    suite = str(run.get("suite") or "").strip().lower()
    if not SUITE_RX.match(suite):
        raise EvalRefused("need_suite", "suite: lowercase letters, digits, . _ -, 2-64 chars")
    paths = run.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise EvalRefused("need_paths", "paths: {<path>: {n, @1, @5, ...}} with at least one path")
    if len(paths) > MAX_PATHS:
        raise EvalRefused("too_many_paths", "at most %d paths per run" % MAX_PATHS)
    clean = {str(k)[:48]: _clean_cells(v) for k, v in paths.items() if isinstance(v, dict)}
    clean = {k: v for k, v in clean.items() if v}
    if not clean:
        raise EvalRefused("need_paths", "no path carried a numeric figure")
    row = {"suite": suite,
           "at": str(run.get("at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))[:25],
           "by": str(by or run.get("by") or "")[:64],
           "machine": str(machine or run.get("machine") or "")[:64],
           "pairs": _count(run.get("pairs")), "excluded": _count(run.get("excluded")),
           "paths": clean, "notes": str(run.get("notes") or "")[:600]}
    for key in ("skipped", "commit", "golden_sha"):
        if run.get(key):
            row[key] = str(run[key])[:300]
    return row


def append(hub_dir, run: dict, *, by: str = "", machine: str = "") -> dict:
    """Validate and append one run; keep the newest KEEP_PER_SUITE of its suite. Returns the
    row as stored (the caller prints it, so a run that did not land never reads as recorded)."""
    row = normalize(run, by=by, machine=machine)
    hub_dir = Path(hub_dir)
    with ProcessFileLock(hub_dir, name=".evals.lock", timeout=30):
        rows = read(hub_dir) + [row]
        same = [i for i, r in enumerate(rows) if r.get("suite") == row["suite"]]
        if len(same) > KEEP_PER_SUITE:
            drop = set(same[:len(same) - KEEP_PER_SUITE])
            rows = [r for i, r in enumerate(rows) if i not in drop]
        atomic.write_text(_path(hub_dir), "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    return row


def trend(hub_dir, suite: str = "", limit: int = 60) -> tuple[list[dict], dict]:
    """(rows newest last, metadata{suites, count, etag}). The etag is over the file's size and
    mtime plus the query, so a caught-up reader can be answered 304."""
    suite = str(suite or "").strip().lower()
    limit = max(1, min(int(limit or 60), KEEP_PER_SUITE))
    try:
        st = _path(hub_dir).stat()
        tag = "e-%d-%d-%s-%d" % (st.st_size, st.st_mtime_ns, suite or "all", limit)
    except OSError:
        tag = "e-empty-%s-%d" % (suite or "all", limit)
    rows = read(hub_dir)
    suites = sorted({r.get("suite") for r in rows if r.get("suite")})
    rows = [r for r in rows if not suite or r.get("suite") == suite][-limit:]
    return rows, {"suites": suites, "count": len(rows), "etag": tag}
