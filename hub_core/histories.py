"""Console chat histories — a bounded, redacted SIDECAR the operator can read to coach prompting.

An operator who guides a team of agent-assisted people wants to read how each console is
actually being driven: what the person asked, what the assistant answered, which tools it
reached for. This module is the hub-side store for that; ``hub_core.transcripts`` is the
workstation side that uploads it.

PRIVACY IS THE DESIGN CONSTRAINT, not an afterthought:

* The feature is OFF unless the adopter turns it on (``HUB_HISTORIES_ENABLED``), and the
  people whose consoles upload must have been told their histories are visible. The uploader
  also honours a per-workstation opt-out marker.
* What is stored is deliberately narrow: prompts, assistant text, ONE short line per tool call
  and a short line per injected message. Never tool output, never model reasoning, never a
  subagent's side conversation — those are dropped on the workstation before upload.
* Text is redacted twice: on the workstation, and again here on receipt, with the same
  credential shapes the write seam refuses (``hub_core.secretscan.redact``). A transcript
  holding a secret is stored with the secret masked; it is not refused, because a history
  with holes where the turns were would be worse than useless.
* It is NOT the ledger. The ledger is append-only and hash-chained, so a secret written there
  could never be removed. Histories live beside it under ``<hub_dir>/histories/`` as one
  ``<agent>--<machine>/<session>.jsonl`` per console — one JSON turn per line plus
  ``{"meta": ...}`` lines (the last one wins) — bounded three ways: a per-console cap (the file
  is compacted to its newest turns), a total cap across consoles (oldest files go first) and a
  retention age. Backups of the hub directory should exclude ``histories/`` unless the adopter
  has decided otherwise.

Reading is the adapter's decision (a scoped credential or an adopter-supplied viewer check);
this module only stores and loads. Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from . import secretscan

MAX_BODY_BYTES = 512 * 1024          # one upload
MAX_SESSIONS_PER_POST = 60
MAX_TURNS_PER_SESSION_POST = 3000
MAX_TEXT = 8000                      # one turn, after the workstation already clipped it
MAX_SESSION_BYTES = 1_500_000        # per console; compacted to ~75% when exceeded
MAX_TOTAL_BYTES = 300 * 1024 * 1024  # every console together; oldest files dropped first
DEFAULT_RETENTION_DAYS = 45
PRUNE_EVERY_S = 900
DEFAULT_LIMIT = 400                  # turns served per read (the newest)
ROLES = ("user", "assistant", "tool", "event")

_LOCK = threading.Lock()
SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{3,79}$")


def root(hub_dir) -> Path:
    return Path(hub_dir) / "histories"


def _console_dir(hub_dir, agent: str, machine: str) -> Path:
    return root(hub_dir) / ("%s--%s" % (agent, machine or "unknown"))


def _write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _compact(path: Path) -> None:
    """Keep the newest turns under ~75% of the per-console cap, and the latest meta."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    meta, turns = None, []
    for line in lines:
        if line.startswith('{"meta"'):
            meta = line
        elif line.strip():
            turns.append(line)
    budget, kept = int(MAX_SESSION_BYTES * 0.75), []
    for line in reversed(turns):
        budget -= len(line.encode("utf-8")) + 1
        if budget < 0:
            break
        kept.append(line)
    kept.reverse()
    head = []
    if meta:
        try:
            m = json.loads(meta)["meta"]
            m["trimmed"] = True
            head = [json.dumps({"meta": m}, ensure_ascii=False)]
        except (ValueError, KeyError, TypeError):
            head = [meta]
    _write_text(path, "\n".join(head + kept) + "\n")


def prune(hub_dir, now: float, retention_days: int = DEFAULT_RETENTION_DAYS,
          force: bool = False) -> dict:
    """Retention age, then the total cap (oldest files first). Throttled by a marker file."""
    base = root(hub_dir)
    marker = base / ".pruned"
    if not force:
        try:
            if now - marker.stat().st_mtime < PRUNE_EVERY_S:
                return {"pruned": 0, "skipped": "throttled"}
        except OSError:
            pass
    files = []
    for folder in base.glob("*--*"):
        if not folder.is_dir():
            continue
        for f in folder.glob("*.jsonl"):
            try:
                st = f.stat()
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, f))
    cutoff = now - retention_days * 86400
    total, removed = sum(size for _m, size, _f in files), 0
    for mtime, size, f in sorted(files, key=lambda t: t[0]):
        if mtime >= cutoff and total <= MAX_TOTAL_BYTES:
            break
        try:
            f.unlink()                      # past retention, or over the total cap
            total -= size
            removed += 1
        except OSError:
            continue
    try:
        base.mkdir(parents=True, exist_ok=True)
        _write_text(marker, str(int(now)))
    except OSError:
        pass
    return {"pruned": removed}


def store(hub_dir, agent: str, machine: str, sessions: list, now: float | None = None,
          retention_days: int = DEFAULT_RETENTION_DAYS) -> dict:
    """Append each session's turns (redacted again) to its sidecar. Returns counts."""
    now = time.time() if now is None else now
    stored, turns_in, redacted = 0, 0, 0
    with _LOCK:
        for item in sessions[:MAX_SESSIONS_PER_POST]:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("session") or "").strip()
            if not SESSION.match(sid):
                continue
            rows = []
            for turn in (item.get("turns") or [])[:MAX_TURNS_PER_SESSION_POST]:
                if not isinstance(turn, dict):
                    continue
                role = str(turn.get("role") or "")
                if role not in ROLES:
                    continue
                # Redact the WHOLE string, then clip: clipping first could cut a secret below
                # the length its shape needs and store the head of it.
                text, hits = secretscan.redact(str(turn.get("text") or "")[:MAX_TEXT * 4])
                text = text[:MAX_TEXT]
                redacted += hits
                if not text.strip():
                    continue
                try:
                    ts = float(turn.get("ts") or 0)
                except (TypeError, ValueError):
                    ts = 0.0
                rows.append(json.dumps({"ts": ts, "role": role, "text": text}, ensure_ascii=False))
            path = _console_dir(hub_dir, agent, machine) / (sid + ".jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            meta = {"agent": agent, "machine": machine, "session": sid,
                    "runtime": str(item.get("runtime") or "")[:16],
                    "cwd": secretscan.redact(str(item.get("cwd") or "")[:260])[0],
                    "title": secretscan.redact(str(item.get("title") or "")[:160])[0],
                    "updated": now}
            if item.get("backlog"):
                meta["backlog"] = True
            block = "\n".join([json.dumps({"meta": meta}, ensure_ascii=False)] + rows) + "\n"
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(block)
            stored += 1
            turns_in += len(rows)
            try:
                if path.stat().st_size > MAX_SESSION_BYTES:
                    _compact(path)
            except OSError:
                pass
        try:
            prune(hub_dir, now, retention_days)
        except OSError:
            pass
    return {"sessions": stored, "turns": turns_in, "redacted_on_receipt": redacted}


def find(hub_dir, agent: str, machine: str, session: str):
    """The sidecar for one console, or None. ``session`` may be a prefix of the id; the newest
    matching file wins. A directory listing, no reads."""
    base = root(hub_dir)
    folders = [_console_dir(hub_dir, agent, machine)] if machine else sorted(base.glob(agent + "--*"))
    best = None
    for folder in folders:
        try:
            for f in os.scandir(folder):
                if f.name.endswith(".jsonl") and f.name.startswith(session):
                    mtime = f.stat().st_mtime
                    if best is None or mtime > best[0]:
                        best = (mtime, Path(f.path))
        except OSError:
            continue
    return None if best is None else best[1]


def consoles(hub_dir, agent: str = "") -> list:
    """Every stored console (newest first): agent, machine, session, bytes, updated. Stats only."""
    out = []
    for folder in root(hub_dir).glob("*--*"):
        if not folder.is_dir():
            continue
        who, _sep, machine = folder.name.partition("--")
        if agent and who != agent:
            continue
        for f in folder.glob("*.jsonl"):
            try:
                st = f.stat()
            except OSError:
                continue
            out.append({"agent": who, "machine": "" if machine == "unknown" else machine,
                        "session": f.name[:-6], "bytes": st.st_size, "updated": st.st_mtime})
    out.sort(key=lambda row: row["updated"], reverse=True)
    return out


def load(path: Path, limit: int = DEFAULT_LIMIT):
    """One sidecar's latest meta and its newest ``limit`` turns, oldest to newest."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    meta, turns = {}, []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if "meta" in row:
            meta = row["meta"] if isinstance(row["meta"], dict) else meta
        else:
            turns.append(row)
    total = len(turns)
    turns = turns[-limit:] if limit > 0 else turns
    return {**meta, "turns": turns, "turn_count": total,
            "served": len(turns), "earlier_not_served": total - len(turns)}
