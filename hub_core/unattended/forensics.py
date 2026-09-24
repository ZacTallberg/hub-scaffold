"""What a pass was actually doing, read off the transcript it wrote.

``claude -p --output-format json`` prints its one JSON object only at EXIT, so a pass the
launcher kills at its ceiling has, by construction, produced zero stdout: the report reads "no
output" whether the runtime hung before its first model call or worked productively for the whole
bound. Those are opposite faults — a broken machine versus a ceiling too small for the item — and
nothing in stdout can tell them apart. The runtime writes its transcript AS IT RUNS, so the
transcript survives the kill.

Claude Code writes one JSONL file per session under ``~/.claude/projects/<slug>/`` where the slug
is the working directory with EVERY non-alphanumeric character replaced by one dash
(``C:\\work`` -> ``C--work``; collapsing runs of them yields a directory that does not exist, and
every lookup then "finds nothing"). The profile is the interpreter's, never a shell's $HOME.

Attribution is by START time, never mtime: the earliest session whose first TIMESTAMPED record
lands inside the launch window is this pass, and anything that opens later in the same directory
is a person's console. (A transcript's head is often an untimestamped summary record, so the first
line alone returns nothing for most real sessions — scan the head for the first stamp.)

Three judgements are read off it:

* ``killed_pass_facts`` — turns, output tokens, the last action, when it began and when it last
  wrote, relative to launch.
* ``working_at_kill`` — still writing within two minutes of the kill, with real turns behind it:
  the slot was too small for the item. That is a WARNING, not a page. It fails closed: no
  transcript, no stamp, or too few turns leaves the report at error.
* ``session_finished`` — the last model turn ended (``stop_reason: end_turn``) AND the runtime
  wrote its shutdown record after it. A pass in that state that has not exited is hung in its own
  shutdown; after a short grace the launcher ends it and records it as FINISHED, not as a ceiling
  kill. Either signal alone is not enough: an end_turn is followed by more turns whenever a tool
  result or hook comes back.

When nothing matches, ``no_evidence`` names WHICH of four causes it was — no projects directory
(a different runtime or profile), an empty one, files none of which were written during the pass,
or files written during it whose first record falls outside the window — because each points at a
different fix, and "the runtime probably never started" is a verdict the check cannot support.
"""
from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path

HEAD_BYTES = 1_000_000
TAIL_BYTES = 400_000
WINDOW_BEFORE_S = 30
WINDOW_AFTER_S = 180
FINISHED_GRACE_S = 60
SHUTDOWN_RECORDS = frozenset({"cost-state"})
WORKING_AT_KILL_S = 120
WORKING_AT_KILL_TURNS = 4


def projects_root() -> Path:
    return Path(os.environ.get("HUB_TRANSCRIPTS_ROOT")
                or Path(os.path.expanduser("~")) / ".claude" / "projects")


def workspace_slug(workspace: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(workspace or "").strip().rstrip("\\/"))


def _stamp(value) -> float:
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def transcript_start(path: Path) -> float:
    """Epoch of the FIRST record carrying a timestamp (not the first line)."""
    try:
        with path.open("rb") as fh:
            raw = fh.read(HEAD_BYTES)
    except OSError:
        return 0.0
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("timestamp"):
            when = _stamp(record["timestamp"])
            if when:
                return when
    return 0.0


def pass_transcript(workspace: str, started: float):
    """(path, start) of the transcript THIS pass is writing, or (None, 0.0)."""
    folder = projects_root() / workspace_slug(workspace)
    if not started or not folder.is_dir():
        return None, 0.0
    best, best_start = None, 0.0
    for candidate in folder.glob("*.jsonl"):
        try:
            info = candidate.stat()
        except OSError:
            continue
        if info.st_mtime < started - 5 or info.st_size <= 0:
            continue
        began = transcript_start(candidate)
        if began and started - WINDOW_BEFORE_S <= began <= started + WINDOW_AFTER_S \
                and (best is None or began < best_start):
            best, best_start = candidate, began
    return best, best_start


def _tail_records(path: Path):
    try:
        raw = path.read_bytes()[-TAIL_BYTES:].decode("utf-8", "replace")
    except (OSError, TypeError):
        return []
    records = []
    for line in raw.splitlines()[1:]:          # the first line is probably severed
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def session_finished(path) -> dict:
    """{} while the session works; {"at", "result", "tokens"} once it has provably finished."""
    end_at, result, tokens, shut = None, "", 0, False
    for record in _tail_records(Path(path)):
        kind = record.get("type")
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        if kind == "assistant":
            tokens += int((message.get("usage") or {}).get("output_tokens") or 0)
            if message.get("stop_reason") == "end_turn":
                end_at = record.get("timestamp")
                result = " ".join(block.get("text") or "" for block in (message.get("content") or [])
                                  if isinstance(block, dict) and block.get("type") == "text")
                shut = False
            else:
                end_at, shut = None, False
        elif kind == "user":
            end_at, shut = None, False
        elif kind in SHUTDOWN_RECORDS and end_at:
            shut = True
    if not (end_at and shut):
        return {}
    return {"at": end_at, "result": result.strip(), "tokens": tokens}


def killed_pass_facts(workspace: str, started: float) -> dict:
    """Turns, output tokens, last action and timing of the pass's own transcript; {} if none."""
    try:
        path, began = pass_transcript(workspace, started)
        if path is None:
            return {}
        turns, tokens, last, last_ts = 0, 0, "", 0.0
        for record in _tail_records(path):
            if record.get("type") in ("user", "assistant"):
                turns += 1
            if record.get("timestamp"):
                last_ts = _stamp(record["timestamp"]) or last_ts
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if isinstance(usage, dict):
                tokens += int(usage.get("output_tokens") or 0)
            if message.get("role") != "assistant":
                continue
            for block in (message.get("content") or []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and str(block.get("text") or "").strip():
                    last = " ".join(str(block["text"]).split())[:240]
                elif block.get("type") == "tool_use":
                    arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
                    what = str(arguments.get("command") or arguments.get("file_path")
                               or arguments.get("pattern") or arguments.get("description") or "")
                    last = "%s(%s)" % (block.get("name") or "tool", " ".join(what.split())[:140])
        if not turns and not last:
            return {}
        return {"transcript": path.name[:8], "turns": turns, "tokens": tokens, "last": last,
                "began_s": int(began - started),
                "last_write_s": int(last_ts - started) if last_ts else None}
    except Exception:  # noqa: BLE001 - evidence must never become a new fault
        return {}


def format_facts(facts: dict) -> str:
    if not facts:
        return ""
    when = "began %+ds from launch" % facts["began_s"]
    if facts.get("last_write_s") is not None:
        when += ", last wrote %+ds from launch" % facts["last_write_s"]
    return ("transcript %s: %d turn(s) in the tail, ~%d output tokens, %s, last action: %s"
            % (facts["transcript"], facts["turns"], facts["tokens"], when,
               facts.get("last") or "(none read)"))


def working_at_kill(facts: dict, elapsed: float):
    """Seconds of silence before the kill when the pass was PROVABLY still working, else None."""
    if not facts or facts.get("last_write_s") is None:
        return None
    if int(facts.get("turns") or 0) < WORKING_AT_KILL_TURNS:
        return None
    idle = int(elapsed) - int(facts["last_write_s"])
    return idle if 0 <= idle <= WORKING_AT_KILL_S else None


def no_evidence(workspace: str, started: float) -> str:
    """What the lookup SAW when it found nothing — which of the four causes, never a verdict."""
    folder = projects_root() / workspace_slug(workspace)
    try:
        if not folder.is_dir():
            return ("no transcript read: %s does not exist, so this runtime is not writing Claude "
                    "Code transcripts under this profile (another runtime, or another HOME) -- the "
                    "pass itself may well have run" % folder)
        files = [f for f in folder.glob("*.jsonl") if f.stat().st_size > 0]
        if not files:
            return "no transcript read: %s holds no non-empty session files" % folder
        live = [f for f in files if f.stat().st_mtime >= started - 5]
        if not live:
            newest = max(files, key=lambda f: f.stat().st_mtime)
            return ("no transcript read: %d session file(s) in %s, none written during the pass "
                    "(newest %s, %ds before launch)" % (len(files), folder, newest.name[:8],
                                                         int(started - newest.stat().st_mtime)))
        offsets = []
        for candidate in live[:6]:
            began = transcript_start(candidate)
            offsets.append("%s %s" % (candidate.name[:8], "start unreadable" if not began
                                      else "started %+ds" % int(began - started)))
        return ("no transcript matched the launch window: %d file(s) in %s were written during the "
                "pass but none began within -%ds..+%ds of launch (%s) -- the pass ran; read those"
                % (len(live), folder, WINDOW_BEFORE_S, WINDOW_AFTER_S, "; ".join(offsets)))
    except Exception as exc:  # noqa: BLE001
        return "no transcript read: the lookup itself failed (%s)" % type(exc).__name__


def clock_text(bound_s: int, margin_min: int = 5) -> str:
    """The run's clock, stated in its own prompt. A pass never told its ceiling starts a
    fifteen-minute suite inside a twenty-minute slot and is killed still writing with nothing
    recorded. The number comes from the same bound the reaper enforces, so it cannot drift."""
    minutes = max(1, int(bound_s) // 60)
    ship_by = max(2, minutes - margin_min)
    return ("YOUR CLOCK: this session is killed %d minutes after launch, hard, mid-command, with no "
            "warning; nothing you have not recorded on the board by then survives. By minute %d, "
            "whatever state you are in: push what you have (a WIP branch if it is not shippable), "
            "record the state on the board (a step, a hand-back note, an answer), and print the "
            "summary line. Never start a command that can outlive the minutes you have left -- "
            "bound long commands with a timeout, exercise only the operation you changed, and if "
            "something can only be proven by a long run, create a task for it instead."
            % (minutes, ship_by))
