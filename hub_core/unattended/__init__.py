"""The unattended responder launcher: one bounded agent session for one board item, then gone.

A scheduler (cron, Task Scheduler, a supervisor) runs ``python -m hub_core.unattended scan
--launch`` every few minutes. It reads the board CHEAPLY, and only when something is genuinely
workable does it spend a session: ``respond <item>`` claims the item on the hub, waits for a lane
on this machine, runs Claude Code or Codex headless against a written charter
(``RESPONDER.md``), and — whatever happened — reads the OUTCOME from the board, never from the
exit code.

What each module owns:

* ``lanes``      — single-flight per lane, held by a heartbeat; a short lane (questions) and a long
                   lane (tasks) so a one-minute answer never waits behind a ninety-minute build; the
                   long lane has machine-sized slots.
* ``runtime``    — which agent runtime runs, its command line, a clean non-interactive environment,
                   and the bounded wait that heartbeats, measures sleep and reaps the process TREE.
* ``faults``     — the lane's own failures told apart from the item's: an account usage limit, a
                   harness that never ran, a run the model API ended. None of them charges an
                   attempt against the item, and each reaches the board.
* ``forensics``  — what a pass killed at its ceiling was actually doing, read off its transcript;
                   a pass that FINISHED but will not exit.
* ``escalation`` — bounded agent-to-agent escalation: hop 1 is retaken once after a cooldown,
                   hop 2 is a person's.
* ``board``      — outcome from the board's state, resume of work a dead run left, hand-back at
                   teardown with proof, the fleet-wide attempt cap.
* ``worktree``   — a task runs in its own git worktree of a dedicated clone, never a person's
                   checkout; kept while it holds anything unpushed, removed only on proof.

Standard library only, like the rest of ``hub_core``. Everything talks to the hub through its
served HTTP seam (``hub_core.client``); nothing here touches the ledger.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


def home() -> Path:
    """This machine's launcher state directory (locks, journal, attempt records).

    Resolved from the INTERPRETER's user profile, never a shell's $HOME, which on some managed
    machines is a network share that hangs or vanishes."""
    raw = os.environ.get("HUB_UNATTENDED_HOME", "").strip()
    path = Path(raw) if raw else Path(os.path.expanduser("~")) / ".hub-unattended"
    path.mkdir(parents=True, exist_ok=True)
    return path


def agent() -> str:
    return (os.environ.get("HUB_AGENT_ID") or "agent").strip()


def machine() -> str:
    return (os.environ.get("HUB_MACHINE") or os.environ.get("COMPUTERNAME")
            or os.environ.get("HOSTNAME") or "this-machine").strip().lower()


def log(message: str) -> None:
    line = "%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), message)
    try:
        with (home() / "launcher.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def read_json(path: Path, default):
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return json.loads(path.read_text(encoding=encoding))
        except (OSError, ValueError):
            continue
    return default


def write_json(path: Path, data) -> None:
    """Atomic replace; fail-soft (bookkeeping must never turn into the failure)."""
    try:
        tmp = path.with_suffix(".tmp.%d" % os.getpid())
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
