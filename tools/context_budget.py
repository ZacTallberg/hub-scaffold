#!/usr/bin/env python3
"""What every prompt actually costs in injected context, read from real session transcripts.

Every hook that fires on a prompt (this hub's `prompt-context` knowledge block, the presence
gate, a local memory tool, a doctrine hook) adds text the model then carries for the rest of the
session. Nobody sees that cost from inside one hook: each side measures only itself. This reads
the agent-session transcripts on this machine and reports, per turn, how many characters each
source injected -- ONE context budget per prompt, measured across every hook on the SAME prompt.

It is a measurement over real transcripts nobody wrote for it, not a test. Stdlib only.

Honest adjustments, all applied here and printed:
  * an output the harness PERSISTED ("Output too large ... Preview (first 2KB)") reached the
    model as a ~2,000-char preview; that is what is counted, and the size that was dropped is
    counted separately, because it is the failure an output ceiling exists to prevent. The
    ceiling and preview apply to a hook's PLAIN stdout only: the merged JSON
    `additionalContext` is delivered whole;
  * a hook that answers in JSON ({"hookSpecificOutput": ...}) is logged twice -- its raw stdout
    and the merged context the model receives. Only the merged copy is counted;
  * turns are split by kind -- a person's prompt, a queued peer / cross-session message, a task
    notification -- since each fires the same hooks for different reasons;
  * a session START is its own kind: every SessionStart hook before the first prompt is one
    row, reported separately and kept OUT of "ALL" (the per-prompt figure), because it is paid
    once per session (and again after each compaction), not once per prompt.

    python tools/context_budget.py [--hours 24] [--root ~/.claude/projects] [--json]
        [--source NAME=REGEX ...]    # classify another hook's output (checked first, in order)

The transcript layout read is Claude Code's JSONL (one file per session under
<root>/<project>/<session>.jsonl, hook output recorded as `attachment` entries). Another runtime
needs its own reader; the report is runtime-neutral.
"""
from __future__ import annotations

import argparse
import calendar
import collections
import json
import os
import re
import statistics
import time
from pathlib import Path

#: What the harness shows of a plain-stdout hook output past HOOK_CEILING.
PREVIEW_CHARS = 2000
#: A hook's plain stdout is shown inline only up to this many chars. The transcript keeps the
#: full text regardless, so the ceiling is applied here rather than read from a marker.
HOOK_CEILING = 10000
_PERSISTED = re.compile(r"Output too large \(([\d.]+)KB\)")
_INJECTING = ("hook_success", "hook_additional_context")

#: The scaffold's own hook outputs, in the order they are tested. Anything unmatched is "other".
DEFAULT_SOURCES = (
    ("hub-knowledge", re.compile(r"<hub-knowledge")),     # client prompt-context: ranked records
    ("hub-live", re.compile(r"<hub-live")),               # client prompt-context: the live block
    ("local-memory", re.compile(r"<recalled-memory|<curated-notes|<memory-")),
    ("doctrine", re.compile(r"<(house-)?doctrine")),
)


def _text(att: dict) -> str:
    for key in ("stdout", "content"):
        v = att.get(key)
        if isinstance(v, list):
            v = "\n".join(str(x) for x in v)
        if v:
            return str(v)
    return ""


def _source(att: dict, sources) -> str:
    """Which system produced this injection, from the hook command or the content itself."""
    probe = str(att.get("command") or "") + "\n" + _text(att)[:4000]
    for name, rx in sources:
        if rx.search(probe):
            return name
    return "other"


def _visible(att: dict, text: str) -> tuple[int, int]:
    """(chars the model saw, chars the harness dropped)."""
    if att.get("type") == "hook_additional_context":
        return len(text), 0                      # merged JSON context arrives whole
    m = _PERSISTED.search(text[:400])
    if m:
        full = int(float(m.group(1)) * 1024)
        return PREVIEW_CHARS, max(0, full - PREVIEW_CHARS)
    if len(text) > HOOK_CEILING:
        return PREVIEW_CHARS, len(text) - PREVIEW_CHARS
    return len(text), 0


def _is_raw_json_copy(att: dict, text: str) -> bool:
    return att.get("type") == "hook_success" and text.lstrip().startswith('{"hookSpecificOutput')


def _kind_of_text(text: str) -> str:
    if "<task-notification>" in text or "[SYSTEM NOTIFICATION" in text:
        return "notification"
    if "<cross-session-message" in text or "<agent-message" in text:
        return "peer-message"
    return "person"


def _prompt_text(entry: dict) -> str:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    return " ".join(str(c.get("text") or "") for c in (content or []) if isinstance(c, dict))


def _epoch(stamp: str) -> float:
    # Transcript timestamps are UTC ("...Z"): timegm, never mktime, which reads local time.
    try:
        return calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0


def measure(root, hours: float, sources=DEFAULT_SOURCES) -> list[dict]:
    """One row per turn: {kind, sources: Counter(src -> chars seen), dropped, ts}."""
    cutoff = time.time() - hours * 3600
    rows: list[dict] = []

    def turn(kind, ts):
        row = {"kind": kind, "sources": collections.Counter(), "dropped": 0, "ts": ts}
        rows.append(row)
        return row

    for path in Path(os.path.expanduser(str(root))).glob("*/*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        current = None
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            ts = str(e.get("timestamp") or "")
            if e.get("type") == "user" and not e.get("isMeta") and not e.get("toolUseResult"):
                content = (e.get("message") or {}).get("content")
                if (isinstance(content, list) and content and isinstance(content[0], dict)
                        and content[0].get("type") == "tool_result"):
                    continue                       # a tool result, not a prompt
                current = turn(_kind_of_text(_prompt_text(e)), ts)
                continue
            att = e.get("attachment")
            if not isinstance(att, dict):
                continue
            if att.get("type") == "queued_command":
                # A message delivered mid-session is its OWN turn: it fires the same prompt
                # hooks, and folding them into the previous prompt would inflate that prompt
                # and hide the peer-message kind.
                kind = _kind_of_text(str(att.get("prompt") or ""))
                current = turn("peer-message" if kind == "person" else kind, ts)
                continue
            event = att.get("hookEvent")
            if event not in ("SessionStart", "UserPromptSubmit") or att.get("type") not in _INJECTING:
                continue
            text = _text(att)
            if not text.strip() or _is_raw_json_copy(att, text):
                continue
            seen, dropped = _visible(att, text)
            if event == "SessionStart":
                # Every SessionStart hook before the next prompt is ONE session-start turn.
                if current is None or current["kind"] != "session-start":
                    current = turn("session-start", ts)
            elif current is None:
                current = turn("person", ts)
            current["sources"][_source(att, sources)] += seen
            current["dropped"] += dropped
    return [r for r in rows if _epoch(r["ts"]) >= cutoff and r["sources"]]


def report(rows: list[dict]) -> dict:
    out = {"prompts": sum(1 for r in rows if r["kind"] != "session-start"),
           "session_starts": sum(1 for r in rows if r["kind"] == "session-start"), "by_kind": {}}
    for kind in ("ALL", "person", "peer-message", "notification", "session-start"):
        # ALL is the per-prompt figure: a session start is paid once, so it is its own row.
        sub = [r for r in rows
               if (r["kind"] != "session-start" if kind == "ALL" else r["kind"] == kind)]
        if not sub:
            continue
        totals = sorted(sum(r["sources"].values()) for r in sub)
        srcs = sorted({s for r in sub for s in r["sources"]})
        out["by_kind"][kind] = {
            "n": len(sub),
            "median_total": int(statistics.median(totals)),
            "p90_total": (int(totals[int(len(totals) * 0.9) - 1]) if len(totals) >= 10
                          else int(totals[-1])),
            "median_by_source": {s: int(statistics.median([r["sources"].get(s, 0) for r in sub]))
                                 for s in srcs},
            "persisted_turns": sum(1 for r in sub if r["dropped"]),
            "dropped_chars_total": sum(r["dropped"] for r in sub),
        }
    return out


def _parse_sources(extra: list[str]):
    parsed = []
    for spec in extra or []:
        name, sep, rx = spec.partition("=")
        if not sep or not name.strip():
            raise SystemExit("--source takes NAME=REGEX, got %r" % spec)
        parsed.append((name.strip(), re.compile(rx)))
    return tuple(parsed) + DEFAULT_SOURCES


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--root", default=os.path.join(os.path.expanduser("~"), ".claude", "projects"))
    ap.add_argument("--source", action="append", default=[], metavar="NAME=REGEX",
                    help="classify another hook's output (tested before the built-in sources)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rep = report(measure(a.root, a.hours, _parse_sources(a.source)))
    if a.json:
        print(json.dumps(rep, indent=1))
        return 0
    print("prompts in the last %gh: %d, session starts: %d (root %s)"
          % (a.hours, rep["prompts"], rep["session_starts"], a.root))
    for kind, r in rep["by_kind"].items():
        print("%-13s n=%-5d median %6d chars  p90 %6d  persisted %d turns (%d chars never seen)"
              % (kind, r["n"], r["median_total"], r["p90_total"], r["persisted_turns"],
                 r["dropped_chars_total"]))
        print("   median by source: " + ", ".join("%s %d" % kv for kv in r["median_by_source"].items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
