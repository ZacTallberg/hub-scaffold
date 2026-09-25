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
                   checkout; kept while it holds anything unpushed, removed only on proof; no
                   URL it reads or builds carries a credential.
* ``publisher``  — the publish hand-off: a machine that PROVED it can push replays a bundle a
                   machine that could not left, rebased and never forced.

Beyond tasks and questions the launcher works NEEDS-ATTENTION conditions the hub marks
``actor: agent``, one at a time in their own lane, cleared only when the hub's list drops them.

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


#: How long a ledger replace keeps retrying a destination another process holds open.
REPLACE_RETRY_S = 5.0


def write_json(path: Path, data) -> bool:
    """Atomic replace that is RETRIED, never silently dropped; returns whether it landed.

    On Windows ``os.replace`` answers WinError 5 while anything holds the destination open -- a
    reader of the ledger, an indexer, an antivirus scanner touching a file just written. A bare
    ``except OSError: pass`` then lost the write without a trace, and a queued or running run
    vanished from the ledger. The retry is bounded by a monotonic DEADLINE (an attempt count
    measures nothing without a clock: with a patched or instant sleep, ten tries take
    microseconds), and a write that still fails is logged. Bookkeeping never raises."""
    from ..atomic import replace
    tmp = path.with_suffix(".tmp.%d" % os.getpid())
    try:
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        replace(tmp, path, timeout_s=REPLACE_RETRY_S)
        return True
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        log("ledger write LOST for %s (%s: %s)" % (path.name, type(exc).__name__, exc))
        return False


def load_env_file(path: Path) -> list:
    """Settings for a scheduled run, which inherits no shell environment: ``KEY=VALUE`` lines,
    only ``HUB_*`` keys, an already-set variable wins. A token pasted into the file is refused;
    ``HUB_AGENT_TOKEN_FILE`` names a file only this user can read, so the credential stays out
    of the task definition, the registry and every process argument."""
    loaded = []
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        lines = []
    for line in lines:
        key, sep, value = line.strip().partition("=")
        key, value = key.strip(), value.strip().strip('"')
        if not sep or line.lstrip().startswith("#") or not key.startswith("HUB_"):
            continue
        if key in ("HUB_AGENT_TOKEN", "HUB_WRITE_TOKEN"):
            continue
        if not os.environ.get(key):
            os.environ[key] = value
            loaded.append(key)
    token_file = os.environ.get("HUB_AGENT_TOKEN_FILE")
    if token_file and not os.environ.get("HUB_AGENT_TOKEN"):
        try:
            os.environ["HUB_AGENT_TOKEN"] = Path(token_file).read_text(encoding="utf-8-sig").strip()
            loaded.append("HUB_AGENT_TOKEN(from file)")
        except OSError:
            pass
    return loaded
