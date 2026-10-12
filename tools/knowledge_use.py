#!/usr/bin/env python3
"""Is the board knowledge pushed into prompts USED? Read from real agent transcripts.

`python -m hub_core.client prompt-context` (wired as a prompt hook) injects a <hub-knowledge>
block of board records into a session. Delivery is easy to count; this measures the other half:
did the AGENT refer to a delivered record later in the same session?

A record counts as USED when its id (``<project>:<type>:<local>``, as the block prints it)
appears AFTER delivery in the agent's own words or actions -- its reply text or a tool call's
input (a ``recall <id>``, a grep, a message). Ids quoted inside later hook injections do not
count: that is the harness repeating itself, not the agent using it.

This UNDER-counts, and says so: knowledge can shape a reply without its id being named. It is a
floor, not the truth -- but unlike "rows delivered", it is a signal a real agent produced. On the
instance this was lifted from, the first reading was a fraction of one percent, which is what
moved knowledge delivery from a per-prompt budget to a relevance cut and to records attached to
the event being worked (a task start, a problem claim).

It also reports the HAND-OFF per prompt: how many prompts carried a knowledge block, how many
carried a block with no memory rows (a repeated focus), and how many carried none.

    python tools/knowledge_use.py [--hours 24] [--root <transcripts dir>] [--json] [--post]

``--post`` records the reading as one run of the board's standing-eval trend (suite
``knowledge-use``, read back at /hub/eval.json) through ``hub_core.client`` (HUB_API_BASE and a
credential holding ``eval:write``), and prints the row the hub STORED -- a post that did not land
must never read as recorded. Beside ``consolidate --post`` (suite ``knowledge-loop``, the
re-learn rate) the trend carries both halves of "is the board's knowledge used, and is it
learning": use should rise while re-learning falls.

``--root`` defaults to Claude Code's per-project transcript folder (~/.claude/projects);
adapt ``_hook_text`` / ``_agent_text`` for another harness's transcript shape. Read-only.
Standard library only.
"""
from __future__ import annotations

import argparse
import calendar
import collections
import json
import os
import re
import sys
import time
from pathlib import Path

_ID = re.compile(r"\b[a-z0-9][a-z0-9_-]*:(?:note|task|adr|feat|gap|directive|cap):[A-Za-z0-9._-]+")
_BLOCK = re.compile(r"<hub-knowledge>.*?</hub-knowledge>", re.S)
_ROW = re.compile(r"^- \*?\[(%s)\]" % _ID.pattern, re.M)


def _epoch(ts) -> float:
    try:
        return calendar.timegm(time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0.0


def _hook_text(entry) -> str:
    """The text a prompt/session hook injected on this transcript line, '' when none."""
    att = entry.get("attachment")
    if isinstance(att, dict) and att.get("hookEvent") in ("UserPromptSubmit", "SessionStart"):
        for key in ("stdout", "content"):
            value = att.get(key)
            if isinstance(value, list):
                value = "\n".join(str(x) for x in value)
            if value:
                return str(value)
    return ""


def _agent_text(entry) -> str:
    """What the AGENT wrote or did on one transcript line: assistant text and tool inputs."""
    msg = entry.get("message") or {}
    if entry.get("type") != "assistant" or not isinstance(msg.get("content"), list):
        return ""
    parts = []
    for c in msg["content"]:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "text":
            parts.append(str(c.get("text") or ""))
        elif c.get("type") == "tool_use":
            parts.append(json.dumps(c.get("input") or {}))
    return "\n".join(parts)


def _delivered(text) -> list:
    """Record ids a block DELIVERED in full on this prompt. The 'already in your context (not
    repeated)' line names ids without delivering them, so it is cut first."""
    out = []
    for block in _BLOCK.findall(text):
        body = block.split("already in your context", 1)[0]
        out += _ROW.findall(body)
    return out


def measure(root, hours) -> dict:
    cutoff = time.time() - hours * 3600
    delivered, used = {}, set()
    prompts = collections.Counter()
    for path in Path(root).glob("*/*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        held = set()
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if _epoch(entry.get("timestamp")) < cutoff:
                continue
            hooked = _hook_text(entry)
            if hooked:
                ids = _delivered(hooked)
                prompts["with_rows" if ids else ("block_no_rows" if "<hub-knowledge>" in hooked
                                                 else "no_block")] += 1
                for rid in ids:
                    if rid not in held:
                        held.add(rid)
                        delivered[(path.name, rid)] = True
                continue
            text = _agent_text(entry)
            if text and held:
                for rid in set(_ID.findall(text)) & held:
                    used.add((path.name, rid))
    return {"hours": hours, "sessions": len({f for f, _ in delivered}),
            "delivered": len(delivered), "used": len(used),
            "used_pct": round(100.0 * len(used) / len(delivered), 2) if delivered else None,
            "prompts": dict(prompts)}


def trend_run(r) -> dict:
    """The reading as one standing-eval run: the use rate over every delivered record, and the
    hand-off (how many prompts carried rows, an empty block, or no block)."""
    p = r["prompts"]
    paths = {"all": {"n": r["delivered"], "delivered": r["delivered"], "used": r["used"]},
             "hand-off": {"n": sum(p.values()), "with_rows": p.get("with_rows", 0),
                          "block_no_rows": p.get("block_no_rows", 0), "no_block": p.get("no_block", 0)}}
    if r["used_pct"] is not None:
        paths["all"]["used_pct"] = r["used_pct"]
    return {"suite": "knowledge-use", "pairs": r["sessions"], "paths": paths,
            "notes": "last %gh of this machine's transcripts; a FLOOR (ids named after delivery)"
                     % r["hours"]}


def post(r) -> int:
    """POST the reading through the hub client and print what the hub stored. 0 stored, 3 refused."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from hub_core import client as _c
    try:
        stored = _c._post(_c._base_urls(None), "eval", trend_run(r)).get("data")
    except (RuntimeError, ValueError) as error:
        print("posted: REFUSED %s" % str(error)[:300])
        return 3
    print("posted: %s" % json.dumps(stored, sort_keys=True)[:600])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--root", default=os.path.join(os.path.expanduser("~"), ".claude", "projects"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--post", action="store_true",
                    help="record the reading on the board's standing-eval trend (suite knowledge-use)")
    a = ap.parse_args()
    r = measure(a.root, a.hours)
    rc = post(r) if a.post else 0
    if a.json:
        print(json.dumps(r, indent=1))
        return rc
    print("last %gh: %d sessions received %d distinct board records; the agent referred to %d of "
          "them by id (%s%%) -- a FLOOR: knowledge can shape a reply unnamed"
          % (a.hours, r["sessions"], r["delivered"], r["used"], r["used_pct"]))
    p = r["prompts"]
    print("hook deliveries: %d with rows, %d with a block but no new rows, %d with no block"
          % (p.get("with_rows", 0), p.get("block_no_rows", 0), p.get("no_block", 0)))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
