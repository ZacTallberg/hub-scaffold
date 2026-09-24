"""Workstation side of console chat histories: read local agent transcripts, upload the new turns.

The hub-side store and the privacy contract are in ``hub_core.histories``; read that first.
This module is what a workstation runs (``python -m hub_core.client history-push [--follow]``)
to send its consoles' NEW turns to the hub.

What leaves the machine, and what does not:
  * the person's prompts and the assistant's text replies — the point of the feature;
  * one short line per tool call (tool name + a short argument summary);
  * a short line for injected or peer messages, so a turn reads in context;
  * NEVER tool outputs, NEVER model reasoning, NEVER a subagent's side conversation.
Every string is redacted HERE, before upload, with ``hub_core.secretscan.redact`` — and the hub
redacts again on receipt.

Transcript formats read today: Claude Code (``~/.claude/projects/*/<session>.jsonl``) and Codex
(``~/.codex/sessions/YYYY/MM/DD/rollout-*-<uuid>.jsonl``). Another runtime is one more parser.

Load discipline: incremental per-file byte cursors, complete lines only, a bounded first read of a
large backlog, a per-pass read budget, at most one batched upload per MIN_INTERVAL_S and only when
some transcript grew, payload capped at MAX_BATCH_BYTES. A failed upload commits no cursor, so
the next pass retries exactly the same turns. Opt out on a workstation by creating
``HISTORY_DISABLED`` in the state directory. Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from . import secretscan

MIN_INTERVAL_S = 60              # at most one upload per minute, whatever the caller's cadence
RECENT_S = 6 * 3600              # a transcript untouched this long is not a live console
BACKLOG_BYTES = 1024 * 1024      # first sight of a big transcript: read only its last 1 MB
READ_BYTES = 1024 * 1024         # per file per pass
PASS_READ_BUDGET = 6 * 1024 * 1024  # all files together per pass; the rest waits a pass
MAX_BATCH_BYTES = 192 * 1024     # one upload body
MAX_TEXT = 6000                  # a prompt or a reply
MAX_TOOL = 180                   # a tool line
MAX_EVENT = 280                  # an injected / peer message line
STATE_NAME = "history-state.json"
OPT_OUT = "HISTORY_DISABLED"
MARK = secretscan.MARK


def state_dir() -> Path:
    """Where the cursors live: ``HUB_HISTORY_STATE_DIR``, else ``~/.hub-history``. Resolved with
    expanduser, so a HOME pointed at a slow network share is not consulted."""
    configured = os.environ.get("HUB_HISTORY_STATE_DIR", "").strip()
    return Path(configured) if configured else Path(os.path.expanduser("~")) / ".hub-history"


def _clip(text, limit):
    """Redact FIRST, then clip — and never clip through the middle of a [REDACTED] mark."""
    text = secretscan.redact(str(text or ""))[0].strip()
    if len(text) <= limit:
        return text
    cut = text[:limit - 2]
    at = cut.rfind("[", max(0, len(cut) - len(MARK)))
    if at >= 0 and text.startswith(MARK, at):
        cut = text[:at] + MARK
    return cut + " …"


def _tool_summary(name: str, args) -> str:
    """``Bash: run the tests`` — the tool and what it was pointed at, never its output."""
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            args = parsed if isinstance(parsed, dict) else args
        except ValueError:
            pass
    arg = ""
    if isinstance(args, dict):
        for key in ("description", "file_path", "path", "pattern", "command", "cmd", "url",
                    "query", "prompt", "skill", "to", "subject"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                arg = value
                break
            if isinstance(value, list) and value and isinstance(value[0], str):
                arg = " ".join(value)
                break
        if not arg:
            for value in args.values():
                if isinstance(value, str) and value.strip():
                    arg = value
                    break
    elif isinstance(args, str):
        arg = args
    arg = " ".join(str(arg).split())
    return _clip(("%s: %s" % (name, arg)) if arg else str(name), MAX_TOOL)


def _ts(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _text_parts(content) -> str:
    if isinstance(content, str):
        return content
    out = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("text", "input_text", "output_text"):
            out.append(str(part.get("text") or ""))
        elif kind in ("image", "input_image"):
            out.append("[image]")
    return "\n".join(x for x in out if x)


def claude_turns(rec: dict, meta: dict) -> list:
    """Turns from one Claude Code transcript line."""
    if not isinstance(rec, dict) or rec.get("isSidechain"):
        return []
    kind = rec.get("type")
    ts = _ts(rec.get("timestamp"))
    if kind == "ai-title" and rec.get("aiTitle"):
        meta["title"] = _clip(rec.get("aiTitle"), 160)
        return []
    if rec.get("cwd"):
        meta["cwd"] = str(rec.get("cwd"))[:260]
    msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
    if kind == "user":
        content = msg.get("content")
        if isinstance(content, list) and any(isinstance(p, dict) and p.get("type") == "tool_result"
                                             for p in content):
            return []                                     # tool OUTPUT: never uploaded
        text = _text_parts(content)
        if not text.strip():
            return []
        if rec.get("isCompactSummary"):
            return [{"ts": ts, "role": "event", "text": "context compacted (summary injected)"}]
        origin = rec.get("origin") if isinstance(rec.get("origin"), dict) else {}
        okind = str(origin.get("kind") or "")
        human = (okind == "human" or rec.get("promptSource") == "typed"
                 or (not okind and not rec.get("isMeta")))
        if human and not text.lstrip().startswith(("<local-command", "<command-message>",
                                                   "<system-reminder>")):
            return [{"ts": ts, "role": "user", "text": _clip(text, MAX_TEXT)}]
        if okind == "peer":
            body = origin.get("body") or text
            who = origin.get("name") or origin.get("from") or "another session"
            return [{"ts": ts, "role": "event",
                     "text": _clip("message from %s: %s" % (who, " ".join(str(body).split())),
                                   MAX_EVENT)}]
        label = okind or ("injected" if rec.get("isMeta") else "system")
        return [{"ts": ts, "role": "event",
                 "text": _clip("%s: %s" % (label, " ".join(text.split())), MAX_EVENT)}]
    if kind == "assistant":
        out = []
        for part in msg.get("content") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and str(part.get("text") or "").strip():
                out.append({"ts": ts, "role": "assistant", "text": _clip(part.get("text"), MAX_TEXT)})
            elif part.get("type") == "tool_use":
                out.append({"ts": ts, "role": "tool",
                            "text": _tool_summary(str(part.get("name") or "tool"), part.get("input"))})
        return out
    return []


def codex_turns(rec: dict, meta: dict) -> list:
    """Turns from one Codex rollout line. The person's prompt is the UserMessage item (the
    response-item user messages also carry injected context)."""
    if not isinstance(rec, dict):
        return []
    ts = _ts(rec.get("timestamp"))
    p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    kind, ptype = rec.get("type"), p.get("type")
    if kind == "session_meta":
        if p.get("cwd"):
            meta["cwd"] = str(p.get("cwd"))[:260]
        return []
    if kind == "event_msg" and ptype == "item_completed":
        item = p.get("item") if isinstance(p.get("item"), dict) else {}
        if item.get("type") == "UserMessage":
            text = _text_parts(item.get("content"))
            if text.strip():
                return [{"ts": ts, "role": "user", "text": _clip(text, MAX_TEXT)}]
        return []
    if kind == "compacted":
        return [{"ts": ts, "role": "event", "text": "context compacted"}]
    if kind != "response_item":
        return []
    if ptype == "message" and p.get("role") == "assistant":
        text = _text_parts(p.get("content"))
        return [{"ts": ts, "role": "assistant", "text": _clip(text, MAX_TEXT)}] if text.strip() else []
    if ptype == "custom_tool_call":
        return [{"ts": ts, "role": "tool",
                 "text": _tool_summary(str(p.get("name") or "tool"), p.get("input"))}]
    if ptype == "function_call":
        return [{"ts": ts, "role": "tool",
                 "text": _tool_summary(str(p.get("name") or "tool"), p.get("arguments"))}]
    return []


_UUID_TAIL = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.I)


def transcripts(now: float, recent_s: float = RECENT_S, home: Path | None = None) -> list:
    """(path, runtime, session id) for every transcript touched in the last ``recent_s``."""
    home = home or Path(os.path.expanduser("~"))
    out = []
    try:
        dirs = [d for d in os.scandir(home / ".claude" / "projects") if d.is_dir()]
    except OSError:
        dirs = []
    for folder in dirs:
        try:
            for f in os.scandir(folder.path):
                if f.name.endswith(".jsonl") and f.is_file() and now - f.stat().st_mtime < recent_s:
                    out.append((f.path, "claude", f.name[:-6]))
        except OSError:
            continue
    sessions = home / ".codex" / "sessions"
    for back in (0, 1):
        day = time.localtime(now - back * 86400)
        folder = sessions / ("%04d" % day.tm_year) / ("%02d" % day.tm_mon) / ("%02d" % day.tm_mday)
        try:
            for f in os.scandir(folder):
                if f.name.startswith("rollout-") and f.name.endswith(".jsonl") and f.is_file() \
                        and now - f.stat().st_mtime < recent_s:
                    match = _UUID_TAIL.search(f.name[:-6])
                    if match:
                        out.append((f.path, "codex", match.group(1).lower()))
        except OSError:
            continue
    return out


def _read_new(path: str, cursor: int, size: int):
    """(complete lines, new cursor). A half-written last line stays for the next pass; a first
    sight of a large file starts at its tail."""
    start = cursor
    if start > size:
        start = 0                                     # truncated or rotated: start over
    first = cursor < 0
    if first:
        start = max(0, size - BACKLOG_BYTES)
    with open(path, "rb") as fh:
        fh.seek(start)
        blob = fh.read(READ_BYTES)
    if first and start > 0:
        newline = blob.find(b"\n")
        if newline < 0:
            return [], start
        blob, start = blob[newline + 1:], start + newline + 1
    end = blob.rfind(b"\n")
    if end < 0:
        return [], start
    return blob[:end + 1].splitlines(), start + end + 1


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def collect(state: dict, now: float, home: Path | None = None) -> tuple:
    """(sessions payload, pending cursors, bytes read) — nothing is committed until upload lands."""
    files = state.setdefault("files", {})
    batch, pending, used, read_total = [], {}, 0, 0
    found = sorted(transcripts(now, home=home), key=lambda t: _mtime(t[0]), reverse=True)
    live = {p for p, _r, _s in found}
    for stale in [p for p in files if p not in live]:
        files.pop(stale, None)
    for path, runtime, sid in found:
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        entry = files.get(path) or {}
        cursor = int(entry.get("offset", -1))
        if cursor == size:
            continue
        if read_total >= PASS_READ_BUDGET:
            break
        try:
            lines, new_cursor = _read_new(path, cursor, size)
        except OSError:
            continue
        read_total += new_cursor - (cursor if cursor >= 0 else max(0, size - BACKLOG_BYTES))
        meta = dict(entry.get("meta") or {})
        parse = codex_turns if runtime == "codex" else claude_turns
        turns = []
        for raw in lines:
            try:
                rec = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            turns.extend(parse(rec, meta))
        pending[path] = {"offset": new_cursor, "meta": meta}
        if not turns:
            continue
        item = {"session": sid, "runtime": runtime, "cwd": meta.get("cwd", ""),
                "title": meta.get("title", ""), "turns": turns,
                "backlog": cursor < 0 and size > BACKLOG_BYTES}
        cost = len(json.dumps(item))
        if batch and used + cost > MAX_BATCH_BYTES:
            pending.pop(path, None)                   # the next pass picks it up
            break
        if cost > MAX_BATCH_BYTES:                    # one huge first read: keep its newest turns
            while turns and len(json.dumps(item)) > MAX_BATCH_BYTES:
                del turns[:max(1, len(turns) // 4)]
            cost = len(json.dumps(item))
        batch.append(item)
        used += cost
    return batch, pending, read_total


def _load_state(folder: Path) -> dict:
    try:
        return json.loads((folder / STATE_NAME).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def _save_state(folder: Path, state: dict) -> None:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / (STATE_NAME + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, folder / STATE_NAME)
    except OSError:
        pass


def push(post, agent: str, *, now: float | None = None, folder: Path | None = None,
         home: Path | None = None, force: bool = False) -> dict:
    """One upload pass. ``post(operation, payload)`` is the client's authenticated POST. Returns a
    small status dict; an ordinary failure is reported, never raised, and commits no cursor."""
    folder = folder or state_dir()
    now = time.time() if now is None else now
    if (folder / OPT_OUT).exists():
        return {"history": "off", "reason": "opted out on this workstation (%s)" % OPT_OUT}
    state = _load_state(folder)
    if not force and now - float(state.get("last_run") or 0) < MIN_INTERVAL_S:
        return {"history": "waiting", "next_in_s": int(MIN_INTERVAL_S - (now - float(state["last_run"])))}
    state["last_run"] = now
    batch, pending, read_total = collect(state, now, home=home)
    result = None
    if batch:
        try:
            result = post("history", {"agent": agent, "sessions": batch})
        except Exception as exc:                      # noqa: BLE001 -- retried next pass
            state["last_error"] = "%s at %d" % (type(exc).__name__, int(now))
            _save_state(folder, state)
            return {"history": "upload_failed", "error": str(exc)[:300], "bytes_read": read_total}
        state["last_upload"] = now
        state["last_error"] = ""
    state.setdefault("files", {}).update(pending)
    _save_state(folder, state)
    return {"history": ("sent %d" % len(batch)) if batch else "idle", "bytes_read": read_total,
            "turns": sum(len(b["turns"]) for b in batch),
            "hub": (result or {}).get("data") if isinstance(result, dict) else None}
