"""python -m hub_core.unattended [--home DIR] <scan|respond|publish|sweep|status|lanes>

    scan [--launch] [--max N]     what an unattended responder here could take (and what it may
                                  not, with why); --launch first runs the publisher tick, then
                                  spawns one `respond` per workable item
    respond <item-id>             one bounded session for exactly this task, question or
                                  needs-attention item, then gone
    publish                       one publish pass: push at most one waiting hand-off, then exit
    sweep                         hand back leases left by runs whose launcher died
    status                        lanes, pauses, the auth disarm, attempt records and recent runs
    lanes                         the lane slots and how the long lane was sized

Environment: HUB_API_BASE, HUB_AGENT_TOKEN (or HUB_WRITE_TOKEN, or HUB_AGENT_TOKEN_FILE),
HUB_AGENT_ID (the operator identity if this launcher answers questions), HUB_MACHINE,
HUB_WORKER_RUNTIME (auto|claude|codex), HUB_UNATTENDED_LONG_SLOTS, HUB_UNATTENDED_HOURLY_CEILING,
HUB_REPO_URL_TEMPLATE, HUB_PUBLISH_HOSTS, HUB_PUBLISH_URL_TEMPLATE, HUB_UNATTENDED_HOME. A
scheduled run inherits no shell: ``--home DIR`` loads ``DIR/unattended.env`` (HUB_* lines only).
See patterns/unattended-responder.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import agent, board, escalation, faults, forensics, home, lanes, load_env_file, log
from . import machine, publisher, read_json, runtime, worktree, write_json
from ..process_lock import _pid_alive

TASK_BOUND_S = 5400        # build, ship, see it deployed: the long lane
SHORT_BOUND_S = 1500       # answer a question, clear one thing
LEASE_TTL_S = 900          # renewed every heartbeat while the run lives; lapses soon after it dies
QUEUE_RECHECK_S = 120      # a launcher waiting for a lane re-reads its item this often
SUSPEND_SLACK_S = 900      # fallback when no heartbeat measured the sleep
SUPERSEDE_CHECK_S = 180    # how often a task run checks whether somebody else took its task
CLAIM_REFRESH_S = 600      # a non-task item's claim is renewed this often while its run lives
ATTENTION_VERIFY_S = 360   # the attention list is served from a snapshot; re-read this long
LAUNCHES_PER_SLOT = 3      # the default hourly ceiling: three launches per long-lane slot
CHARTER = Path(__file__).resolve().parent / "RESPONDER.md"

# NEEDS ATTENTION IS WORK. The hub marks each condition ``actor: agent|person``; an agent item is
# offered here and cleared ONLY when the hub stops listing it -- never when the session says so.
ATTENTION_BRIEF = """
THIS IS A NEEDS-ATTENTION ITEM ({ATTENTION_KIND}). The hub computed it from live state and drops
it by itself the moment the condition is gone -- that is the ONLY proof you cleared it. Fix the
condition, not the message. Its "Fix:" line is the hub's best instruction; follow it, and go
further where it stops.
- A TASK condition (a task needing a person, a complete-but-unclosed task, a stalled or orphaned
  task or lease): read the task and every checkpoint, then prove the outcome yourself on the
  DEPLOYED system -- the served commit, the pipeline's jobs, the page or endpoint the task is
  about. Reached: finish it with that evidence. Not reached and the remaining work is clear: do
  it, ship it through the normal path, verify, finish. A LIVE attended console holds it: do not
  take it; message that console with what you found.
- An APP condition (dark, partial, down, a failed deploy): work in the app's repository, wire
  what the gap names, push, wait for the pipeline, and prove it on the deployed app.
- A HUB or MACHINE condition: fix it where the item names, then read the attention list again
  and see it gone.
- The few things that are a person's (an approval the operator reserved, trusting a runtime's
  hooks, minting or rotating a credential, deleting files on a shared host) are never yours. If
  the ONLY way forward is one of them, say exactly which one in your final message and stop.
"""


def _bound(kind: str) -> int:
    long_clock = kind in ("task", "attention")
    name = "HUB_UNATTENDED_TASK_BOUND_S" if long_clock else "HUB_UNATTENDED_SHORT_BOUND_S"
    try:
        return max(60, int(os.environ.get(name) or 0)) if os.environ.get(name) else (
            TASK_BOUND_S if long_clock else SHORT_BOUND_S)
    except ValueError:
        return TASK_BOUND_S if long_clock else SHORT_BOUND_S


def hourly_ceiling() -> int:
    """Sessions this machine may START per rolling hour: ``HUB_UNATTENDED_HOURLY_CEILING``, else
    three per long-lane slot (never below eight)."""
    try:
        explicit = int(os.environ.get("HUB_UNATTENDED_HOURLY_CEILING") or 0)
    except ValueError:
        explicit = 0
    if explicit > 0:
        return explicit
    return max(8, LAUNCHES_PER_SLOT * lanes.long_lane_slots()[0])


def attention_ceiling() -> int:
    """NEEDS-ATTENTION IS THE LOWEST LANE: at most a third of the hourly ceiling, at least one.
    An attention backlog otherwise uses the whole hour within minutes of a lane arming and
    starves the tasks and questions it runs beside."""
    return max(1, hourly_ceiling() // 3)


# ---------------------------------------------------------------- pauses and the auth disarm

def disabled() -> str:
    """The kill switch: ``<home>/DISABLED`` exists -> nothing launches and nothing publishes."""
    flag = home() / "DISABLED"
    return ("kill switch: %s exists" % flag) if flag.exists() else ""


def _pause_path() -> Path:
    return home() / "pause.json"


def paused() -> str:
    state = read_json(_pause_path(), {})
    until = float(state.get("until") or 0)
    if until <= time.time():
        return ""
    return "%s; launches resume after %s" % (state.get("reason") or "paused",
                                              time.strftime("%H:%M", time.localtime(until)))


def pause(until: float, reason: str, announce: bool = False) -> None:
    """Park every launch on this machine until ``until``. Said ONCE on the board when
    ``announce`` (the faults that already filed their own row pass False)."""
    already = float(read_json(_pause_path(), {}).get("until") or 0) > time.time()
    write_json(_pause_path(), {"until": until, "reason": reason[:200], "at": time.time()})
    when = time.strftime("%H:%M", time.localtime(until))
    log("lane paused until %s: %s (no attempt charged)" % (when, reason[:160]))
    if announce and not already:
        board.report_fault("unattended_lane_paused",
                           "the unattended lane on this machine is paused until %s" % when,
                           "%s\nNothing was charged against any item; queued work resumes "
                           "then." % reason, severity="warning")


def _auth_path() -> Path:
    return home() / "auth-expired.json"


def credential_files() -> list[Path]:
    """Where the installed runtimes keep their login: a login REWRITES one of these."""
    base = Path(os.path.expanduser("~"))
    return [base / ".claude" / ".credentials.json", base / ".codex" / "auth.json"]


def auth_expired() -> str:
    """Non-empty while this machine's agent login is known to have expired.

    Cleared by the one human action that fixes it: a login rewrites the runtime's credential
    file, so a credential file NEWER than the disarm is the proof a person logged in again. Read
    on EVERY scheduled tick, so a renewed login re-arms the machine within one interval, queue or
    no queue. Deleting the marker by hand re-arms it too."""
    record = read_json(_auth_path(), None)
    if not isinstance(record, dict):
        return ""
    at = float(record.get("at") or 0)
    for cred in credential_files():
        try:
            if cred.is_file() and cred.stat().st_mtime > at + 5:
                _auth_path().unlink()
                log("auth: the agent login on this machine was renewed (%s rewritten); the lane "
                    "is armed again" % cred.name)
                return ""
        except OSError:
            continue
    return str(record.get("text") or "auth-expired")


def disarm_auth(text: str, what: str) -> None:
    """The FIRST auth-class failure disarms the lane until a person logs in again.

    Not a pause: a credential nobody has renewed does not renew itself, and a lane that only
    paused burned the next item on the same dead login every half hour. Reported ONCE, naming
    the single human action."""
    first = read_json(_auth_path(), None) is None
    write_json(_auth_path(), {"at": time.time(), "text": text[:160], "what": what[:120],
                              "runtime": runtime.selected()})
    remedy = ("run `%s` interactively on %s and log in" % (runtime.selected(), machine()))
    log("auth: %s failed on an expired login (%s); the lane is DISARMED until a person logs in: %s"
        % (what, text, remedy))
    if first:
        board.report_fault("unattended_auth_expired",
                           "the agent login on %s has expired; the unattended lane is disarmed "
                           "-- %s" % (machine(), remedy),
                           "first failure: %s\nmatched: %s\nNothing launches here until the "
                           "login is renewed; the credential file being rewritten re-arms it by "
                           "itself." % (what, text), severity="critical")


# ---------------------------------------------------------------- one run per item per machine

_HELD_ITEM_LOCK: Path | None = None


def acquire_item_lock(item_id: str) -> bool:
    """ONE RUN PER ITEM PER MACHINE. The item claim is per MACHINE (a same-machine re-claim is
    granted, so a queued launcher can refresh it) and the lanes have several slots, so two
    launchers for the SAME item on one machine could both run: a task is saved by its lease, a
    needs-attention condition by nothing. A file per item holds the launcher's pid; a lock whose
    pid is gone is a dead run's and is taken over."""
    global _HELD_ITEM_LOCK
    locks = home() / "item-locks"
    path = locks / (hashlib.sha256(item_id.encode("utf-8")).hexdigest()[:20] + ".lock")
    try:
        locks.mkdir(parents=True, exist_ok=True)
    except OSError:
        return True                   # cannot keep locks here at all: the lanes still bound it
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                holder = int((path.read_text(encoding="utf-8").strip() or "0").split()[0])
            except (OSError, ValueError):
                holder = 0
            if holder and holder != os.getpid() and _pid_alive(holder):
                return False
            try:
                path.unlink()         # its launcher is gone: a dead run's lock
            except OSError:
                return False
            continue
        except OSError:
            return True
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("%d %s" % (os.getpid(), item_id))
        _HELD_ITEM_LOCK = path
        return True
    return False


def release_item_lock() -> None:
    global _HELD_ITEM_LOCK
    path, _HELD_ITEM_LOCK = _HELD_ITEM_LOCK, None
    if path is not None:
        try:
            path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- the prompt

def _uncommitted(directory: str) -> str:
    try:
        root = Path(directory)
        children = [root] if (root / ".git").exists() else sorted(p for p in root.iterdir() if p.is_dir())
        parts = []
        for child in children[:200]:
            if not (child / ".git").exists():
                continue
            result = runtime.git(["-C", child, "status", "--porcelain"], timeout=15)
            count = len([ln for ln in (result.stdout or "").splitlines() if ln.strip()])
            if count:
                parts.append("%s: %d file%s" % (child.name, count, "" if count == 1 else "s"))
            if len(parts) >= 5:
                break
        return "; ".join(parts)
    except OSError:
        return ""


def _prompt(item: dict, bound_s: int, tree: dict) -> str:
    charter = CHARTER.read_text(encoding="utf-8")
    charter = (charter.replace("{AGENT}", agent()).replace("{MACHINE}", machine())
                      .replace("{CLOCK}", forensics.clock_text(bound_s)))
    if item["kind"] == "task":
        block = ("\nTHE ITEM (task):\nid: %s\ntitle: %s\npriority: %s\nacceptance (definition of "
                 "done): %s\n%s%s" % (
                     item["id"], item.get("title") or "", item.get("priority") or "",
                     item.get("acceptance") or "",
                     ("plan so far:\n" + item["plan_text"] + "\n") if item.get("plan_text") else "",
                     "RESUMING: a run already worked this task and left it in progress; start "
                     "from what its plan records.\n" if item.get("resume") else ""))
    elif item["kind"] == "attention":
        block = ("\nTHE ITEM (needs attention):\nid: %s\n%s\n" % (item["id"], item.get("body") or "")
                 + ATTENTION_BRIEF.replace("{ATTENTION_KIND}", item.get("attention_kind") or "?"))
    else:
        block = ("\nTHE ITEM (question%s):\nid: %s\nfrom: %s\nquestion: %s\ncontext:\n%s\n" % (
            ", escalation hop %d" % escalation.hop_of(item) if escalation.hop_of(item) else "",
            item["id"], item.get("from") or "?", item.get("title") or "", item.get("body") or ""))
    return charter + block + worktree.brief(tree)


def _details(item_id: str, kind: str, rc, verdict: dict, elapsed: float, out: str,
             timing: dict, workspace: str, started: float) -> str:
    tail = " ".join(str(verdict.get("result") or out or "").split())[-900:]
    detail = ("item=%s kind=%s rc=%s tokens=%d turns=%d duration=%ds machine=%s runtime=%s"
              % (item_id, kind, rc, int(verdict.get("tokens") or 0), int(verdict.get("turns") or 0),
                 int(elapsed), machine(), runtime.selected()))
    if timing.get("session_s"):
        asleep = int(timing.get("suspended_s") or 0)
        detail += ("\nwhere the time went: session %ds to the %s (%s), kill-to-reaped %ds (%s)"
                   % (timing["session_s"], "kill" if rc is None else "exit",
                      ("~%ds of it the machine was ASLEEP" % asleep) if asleep else "no sleep measured",
                      int(timing.get("kill_s") or 0), timing.get("reaped") or "?"))
    detail += "\nworker tail: " + (tail or "(no output)")
    if rc is None and not timing.get("superseded"):
        detail += "\nwhat it was doing: " + (
            forensics.format_facts(forensics.killed_pass_facts(workspace, started))
            or forensics.no_evidence(workspace, started))
    return detail


# ---------------------------------------------------------------- respond: one item, then gone

def respond(item_id: str, workspace: str, repo_url: str = "") -> dict:
    """One bounded session for ONE item -- and never two at once for the same item here."""
    if not acquire_item_lock(item_id):
        log("respond: %s is already being worked by another launcher on this machine; standing "
            "down (nothing charged)" % item_id)
        return {"item": item_id, "outcome": "already-running-here"}
    try:
        return _respond(item_id, workspace, repo_url)
    finally:
        release_item_lock()


def _respond(item_id: str, workspace: str, repo_url: str = "") -> dict:
    swept = board.sweep_orphans()
    if swept:
        log("sweep: %s" % swept)
    reason = disabled() or paused()
    if reason:
        log("respond: %s stays pending: %s" % (item_id, reason))
        return {"item": item_id, "outcome": "paused", "why": reason}
    expired = auth_expired()
    if expired:
        log("respond: %s stays pending: the agent login here expired (%s)" % (item_id, expired))
        return {"item": item_id, "outcome": "disarmed", "why": "agent login expired: " + expired}

    records = board.responses()
    record = records.get(item_id) or {}
    if record.get("state") == "done":
        if not board.stale_done(item_id, record):
            return {"item": item_id, "outcome": "already-cleared"}
        log("respond: %s has a stale local 'done' the board contradicts; dropped, attempt refunded"
            % item_id)
        record = {"attempts": max(0, int(record.get("attempts") or 1) - 1)}
    if record.get("state") == "running" and time.time() - float(record.get("at") or 0) > 2 * TASK_BOUND_S:
        log("respond: %s has a 'running' record from a dead run; attempt refunded" % item_id)
        record["attempts"] = max(0, int(record.get("attempts") or 1) - 1)
    waiting = board.deferred(item_id)
    if waiting:
        return {"item": item_id, "outcome": "deferred", "why": waiting}
    attempts = int(record.get("attempts") or 0)
    now = time.time()
    if attempts >= board.MAX_ATTEMPTS:
        log("respond: %s already attempted %d times here; left for a person" % (item_id, attempts))
        board.flag_needs_person(item_id, "attempted %d times on %s without clearing it (the "
                                "per-machine cap)" % (attempts, machine()))
        # A REFUSAL IS A RESPONSE: without one the item is offered and refused every cycle.
        board.defer(item_id, now + board.CAPPED_RECHECK_S, "at the attempt cap; a person's",
                    attempts)
        return {"item": item_id, "outcome": "attempts-exhausted"}
    if board.launches_in_last_hour() >= hourly_ceiling():
        board.defer(item_id, now + board.CEILING_RECHECK_S, "hourly ceiling", attempts)
        return {"item": item_id, "outcome": "deferred", "why": "hourly ceiling reached"}
    if item_id.startswith(board.ATTENTION_PREFIX) and \
            board.launches_in_last_hour(board.ATTENTION_PREFIX) >= attention_ceiling():
        board.defer(item_id, now + board.CEILING_RECHECK_S,
                    "the attention share of the hourly ceiling", attempts)
        return {"item": item_id, "outcome": "deferred",
                "why": "attention share of the hourly ceiling reached"}

    item = board.resolve(item_id)
    if item and item.get("kind") == "unavailable":
        return {"item": item_id, "outcome": "unverified", "why": "the hub could not be read"}
    if item is None:
        if item_id.startswith(board.ATTENTION_PREFIX) and \
                board.attention_standing(item_id) is not False:
            # Not offered is not cleared: the list still shows it (or cannot say). No record.
            return {"item": item_id, "outcome": "waiting",
                    "why": "not offered to an agent here, and the hub has not dropped it"}
        state = "stood-down"
        if ":task:" in item_id and board.task_status(item_id) == "done":
            state = "done"
        if item_id.startswith(board.ATTENTION_PREFIX):
            state = "done"                                  # the list dropped it: gone
        records[item_id] = {**record, "state": state, "at": time.time()}
        board.save_responses(records)
        return {"item": item_id, "outcome": state, "why": "no longer offered here"}
    kind = item["kind"]
    if kind == "question":
        ok, why = escalation.workable(item)
        if not ok:
            log("respond: %s not taken: %s" % (item_id, why))
            age = item.get("age_s")
            if escalation.hop_of(item) == 1 and age is not None \
                    and int(age) < escalation.COOLDOWN_S:
                board.defer(item_id, now + escalation.COOLDOWN_S - int(age) + 30,
                            "escalation cooldown", attempts)
            return {"item": item_id, "outcome": "waiting", "why": why}
    elif kind == "task":
        capped = board.at_run_cap(item)
        if capped:
            board.flag_needs_person(item_id, capped + " without closing")
            board.defer(item_id, now + board.CAPPED_RECHECK_S, capped + "; a person's", attempts)
            return {"item": item_id, "outcome": "left-for-a-person", "why": capped}
        if item.get("pipeline_wait"):
            # A hand-back whose pushed commit still has a pipeline running: nothing new to read.
            return {"item": item_id, "outcome": "waiting", "why": item["pipeline_wait"]}

    # CLAIM FIRST for anything that is not a task (a task has its fenced lease): before a run is
    # queued, so a machine that loses the race writes no ledger row and spends nothing.
    claimed = kind != "task"
    if claimed:
        verdict = board.item_claim(item_id)
        if verdict.startswith("held:"):
            log("respond: %s is being worked by %s; standing down" % (item_id, verdict[5:]))
            board.defer(item_id, now + board.HELD_RECHECK_S, "held by " + verdict[5:], attempts)
            return {"item": item_id, "outcome": "stood-down", "why": "held by " + verdict[5:]}
        if verdict == "unreachable":
            log("respond: the item-claim route is unreachable; proceeding WITHOUT a claim on %s "
                "(a rare double-fix beats a problem nobody fixes)" % item_id)

    def stand_down(run_id: str, why: str, defer_s: int = 0) -> dict:
        """A run that ends before any session spent anything leaves no ledger row."""
        board.drop_run(run_id)
        if claimed:
            board.item_claim(item_id, release=True)
        if defer_s:
            board.defer(item_id, time.time() + defer_s, why, attempts)
        return {"item": item_id, "outcome": "stood-down", "why": why}

    bound = _bound(kind)
    lane = lanes.lane_for(kind)
    run_id = board.new_run_id()
    leases_file = str(home() / (run_id + ".leases.jsonl"))
    board.record_run(run_id, item=item_id, kind=kind, lane=lane, bound_s=bound,
                     state="queued", queued=time.time(), leases_file=leases_file,
                     attempt=attempts + 1)
    lock = lanes.LaneLock(lane)
    wait_until, recheck_at = time.time() + 2 * bound, time.time() + QUEUE_RECHECK_S
    while not lock.acquire():
        if time.time() >= recheck_at:
            recheck_at = time.time() + QUEUE_RECHECK_S
            if kind == "task" and board.resolve(item_id) is None:
                return stand_down(run_id, "taken or closed while queued for a lane")
            if claimed and board.item_claim(item_id).startswith("held:"):
                return stand_down(run_id, "claimed elsewhere while queued", board.HELD_RECHECK_S)
        if time.time() >= wait_until:
            board.record_run(run_id, state="ended", outcome="lane-full", ended=time.time())
            if claimed:
                board.item_claim(item_id, release=True)
            return {"item": item_id, "outcome": "lane-full"}
        time.sleep(15)

    token, tree, started = "", {}, time.time()
    try:
        board.record_run(run_id, state="running", lane_slot=lock.path.name if lock.path else "")
        item = board.resolve(item_id)            # the wait may have been long: re-read it
        if not item or item.get("kind") == "unavailable":
            return stand_down(run_id, "changed while queued")
        session_dir = workspace
        env_extra = {"HUB_RUN_ID": run_id, "HUB_RUN_KIND": kind, "HUB_RUN_ITEM": item_id,
                     "HUB_RUN_LEASES": leases_file,
                     "HUB_RESPONDER_HOP": escalation.next_hop(item),
                     "PYTHONPATH": os.pathsep.join(filter(None, [
                         str(Path(__file__).resolve().parents[2]), os.environ.get("PYTHONPATH")]))}
        if kind == "task":
            token, claim = board.claim(item_id, LEASE_TTL_S)
            if not token:
                return stand_down(run_id, "the hub refused the claim: %s" % str(claim)[:200],
                                  board.HELD_RECHECK_S)
            with open(leases_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"task": item_id, "token": token, "at": time.time()}) + "\n")
            env_extra["HUB_LEASE_TOKEN"] = token
            session_dir, tree = worktree.prepare(item_id, item.get("title") or "", workspace, repo_url)
            board.record_run(run_id, workspace=session_dir, worktree=tree.get("path", ""))
            if tree.get("url"):
                # The resolved repository goes on the task, once: the next run, a deploy record
                # and a commit resolver need it, and a title slug is a guess.
                board.record_project(item_id, worktree.project_of(tree["url"]), token=token)
        exe = runtime.executable()
        if not exe:
            raise RuntimeError("the %s runtime is not installed on this machine" % runtime.selected())
        records = board.responses()
        records[item_id] = {"state": "running", "at": time.time(), "attempts": attempts + 1}
        board.save_responses(records)
        prompt = _prompt(item, bound, tree)
        watch = {"next": time.time() + SUPERSEDE_CHECK_S, "strikes": 0, "held_by": "",
                 "claim_at": time.time()}

        def beat():
            """Renew what this run holds, and say when somebody else took its task over.

            The proof is the FENCING TOKEN, never a session-id prefix: only the lease holder can
            finish a task, so a heartbeat that keeps succeeding up to a `done` means THIS run
            finished it. A refused heartbeat with the task in progress under another holder, on
            two checks a SUPERSEDE_CHECK_S apart (never on one blip), is a run working for
            nobody -- and a task that then reads `done` was finished elsewhere."""
            lock.beat()
            if claimed and time.time() - watch["claim_at"] >= CLAIM_REFRESH_S:
                watch["claim_at"] = time.time()
                board.item_claim(item_id)
            if not token:
                return None
            ours = board.heartbeat(item_id, token, LEASE_TTL_S)
            if ours or time.time() < watch["next"]:
                if ours:
                    watch["strikes"] = 0
                return None
            watch["next"] = time.time() + SUPERSEDE_CHECK_S
            data = board.task(item_id)
            if not data:
                return None                                  # unreadable: fail open
            holder = data.get("holder") if isinstance(data.get("holder"), dict) else {}
            if data.get("status") == "in_progress" and holder:
                watch["strikes"] += 1
                watch["held_by"] = str(holder.get("agent") or "another holder")
                if watch["strikes"] >= 2:
                    return "the task is held by %s" % watch["held_by"]
                return None
            if data.get("status") == "done" and watch["strikes"]:
                return "the task was finished elsewhere (by %s)" % watch["held_by"]
            watch["strikes"] = 0
            return None

        log("respond: launching %s for %s (%s, bounded %ds, %s)" % (
            runtime.selected(), item_id, kind, bound, lock.path.name if lock.path else "?"))
        board.record_run(run_id, launched=time.time(), runtime=runtime.selected())
        started = time.time()
        rc, out, timing = runtime.run_bounded(
            runtime.command(prompt), workspace=session_dir, bound_s=bound,
            env=runtime.worker_env(env_extra), on_beat=beat)
        elapsed = time.time() - started
        verdict = faults.classify(rc, out)
        superseded = str(timing.get("superseded") or "")
        suspended = int(timing.get("suspended_s") or 0) if rc is None else 0
        if rc is None and not suspended and not superseded and elapsed > bound + SUSPEND_SLACK_S:
            suspended = int(elapsed - bound)

        if kind == "task":
            result = board.outcome(board.task_status(item_id))
        elif kind == "attention":
            # The hub's LIST decides, re-read on a slow clock because it is served from a
            # snapshot. Unknown is never cleared: it is `unverified`, and the attempt refunded.
            standing = board.attention_standing(item_id)
            deadline = time.time() + ATTENTION_VERIFY_S
            while standing is not False and time.time() < deadline:
                time.sleep(30)
                standing = board.attention_standing(item_id)
            result = ("cleared" if standing is False else "unverified" if standing is None
                      else "not-cleared")
        else:
            still = board.resolve(item_id)
            result = "cleared" if still is None else "not-cleared"
        if superseded:
            # Stopped because somebody ELSE finished or took the task: nothing of ours is left on
            # it, so it is neither handed back nor charged -- and not `cleared` either.
            result = "superseded"
        elif result not in ("cleared", "unverified") and rc is None:
            result = "suspended" if suspended else "timed-out"
        lane_fault = verdict["kind"] in ("usage-limit", "harness-dead", "api-error")
        if lane_fault and result not in ("cleared", "superseded"):
            result = "lane-fault:" + verdict["kind"]

        # Attempts: a lane fault, a superseded run and an unverifiable one are not tries.
        records = board.responses()
        entry = records.get(item_id) or {"attempts": 1}
        entry.update({"at": time.time(), "tokens": verdict["tokens"], "rc": rc,
                      "state": "done" if result == "cleared" else result})
        if lane_fault or result in ("superseded", "unverified"):
            entry["attempts"] = max(0, int(entry.get("attempts") or 1) - 1)
        records[item_id] = entry
        board.save_responses(records)

        details = _details(item_id, kind, rc, verdict, elapsed, out, timing, session_dir, started)
        if verdict.get("auth_expired"):
            disarm_auth(verdict["auth_expired"], "the run for %s" % item_id)
        elif verdict["kind"] == "usage-limit":
            pause(verdict["until"], "the account hit its usage limit (%s)" % verdict["reason"],
                  announce=True)
        elif verdict["kind"] == "api-error":
            board.report_fault("unattended_api_error",
                               "the model API ended an unattended run on this machine; attempt "
                               "refunded, item back on the queue", details, severity="warning")
            pause(verdict["until"], verdict["reason"])
        elif verdict["kind"] == "harness-dead":
            board.report_fault("unattended_lane_failed",
                               "the unattended lane could not run: %s" % verdict["reason"],
                               details, severity="critical")
            pause(verdict["until"], verdict["reason"])
        elif superseded:
            log("respond: %s run stopped: %s; nothing charged" % (item_id, superseded))
        elif rc is None and result != "cleared":
            facts = forensics.killed_pass_facts(session_dir, started)
            idle = forensics.working_at_kill(facts, elapsed)
            if suspended:
                message = ("an unattended run was killed on wake: the machine slept ~%dm under its "
                           "%ds bound" % (suspended // 60, bound))
                severity = "warning"
            elif idle is not None:
                message = ("an unattended run ran out of clock while still working (%d turns, "
                           "~%d output tokens, still writing %ds before the kill)"
                           % (facts["turns"], facts["tokens"], idle))
                severity = "warning"
            elif timing.get("reaped") != "reaped":
                message, severity = "an unattended run's process tree SURVIVED its kill", "critical"
            else:
                message, severity = "an unattended run was killed at its %ds ceiling" % bound, "error"
            board.report_fault("unattended_pass_timed_out", message, details, severity=severity)

        # Teardown: every lease this run held and left in progress goes back to the queue, with
        # the token as proof -- carrying the failing evidence, and marked IDLE when the run did no
        # new work (so it does not count toward the task's run cap).
        idle_task, evidence = "", ""
        if kind == "task" and result not in ("cleared", "superseded"):
            data = board.task(item_id) or {}
            evidence = board.failing_evidence(data)
            if data and not board.did_new_work(data, started):
                idle_task = item_id
        note = ("The unattended run holding this task ended (%s) with the task unfinished; handed "
                "back by its launcher. The steps above carry what it recorded.%s"
                % (result.replace("left-active", "its clock ran out" if rc is None else "it exited"),
                   (" " + evidence) if evidence else ""))
        freed = board.release_left(leases_file, note,
                                   skip=item_id if result == "superseded" else "",
                                   uncharged=lane_fault and result != "cleared",
                                   idle_task=idle_task)
        if kind == "task" and result == "left-active" and any(f["task"] == item_id for f in freed):
            result = "handed-back"
        if claimed:
            board.item_claim(item_id, release=True)         # given back the moment the run ends
        dirty = _uncommitted(session_dir)
        settled = worktree.settle(tree, "cleared" if result == "cleared" else result) if tree else ""
        pruned = worktree.prune(workspace, item_id.rsplit(":task:", 1)[0], skip=tree.get("path", "")) \
            if kind == "task" else []
        run = board.record_run(run_id, state="ended", ended=time.time(), rc=rc, outcome=result,
                               session=verdict["session"], tokens=verdict["tokens"],
                               turns=verdict["turns"], timing=timing, suspended_s=suspended,
                               freed=freed, uncommitted=dirty, worktree_settled=settled,
                               pruned=pruned, fault=verdict["kind"] if lane_fault else "",
                               superseded=superseded or None, idle=bool(idle_task),
                               push=publisher.push_state())
        if dirty and result not in ("cleared", "superseded"):
            board.report_fault("unattended_left_uncommitted_work",
                               "an unattended run ended with uncommitted work",
                               "item=%s outcome=%s\nuncommitted: %s\n%s" % (
                                   item_id, result, dirty, settled), severity="warning")
        log("respond: %s -> %s (%s tokens, %s)" % (item_id, result, verdict["tokens"],
                                                  settled or "no worktree"))
        return run
    except Exception as exc:  # noqa: BLE001 - the lane's own crash is reported, never silent
        board.report_fault("unattended_launcher_crashed",
                           "the unattended launcher failed: %s" % type(exc).__name__,
                           "item=%s\n%s" % (item_id, worktree.redact(str(exc))[:1500]))
        freed = board.release_left(leases_file, "The unattended launcher failed before the run "
                                    "finished; handed back so the task is not held by nobody.")
        if claimed:
            board.item_claim(item_id, release=True)
        return board.record_run(run_id, state="ended", ended=time.time(), outcome="launcher-failed",
                                error="%s: %s" % (type(exc).__name__, str(exc)[:300]), freed=freed)
    finally:
        lock.release()


def scan(launch: bool, limit: int, workspace: str) -> dict:
    expired = auth_expired()                     # every tick: a renewed login re-arms here
    found = {}
    off = disabled()
    if launch and not off:
        # PUBLISH WHAT MACHINES THAT CANNOT PUSH HANDED OFF, before the queue: it is not a model
        # session and is not paced like one, and finished work waiting on a push is the most
        # expensive thing on this tick.
        try:
            found["publisher"] = publisher.tick(workspace)
        except Exception as exc:  # noqa: BLE001 - the publisher never takes the scan down
            found["publisher"] = {"error": "%s: %s" % (type(exc).__name__, worktree.redact(str(exc)))}
    found.update(board.scan())
    found["paused"] = off or paused()
    found["disarmed"] = ("the agent login here expired (%s)" % expired) if expired else ""
    found["launched"] = []
    if launch and not found["paused"] and not expired:
        for row in found["workable"][:max(0, limit)]:
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | runtime.NO_WINDOW if os.name == "nt" else 0
            python = sys.executable
            if os.name == "nt" and python.lower().endswith("pythonw.exe"):
                python = python[:-len("pythonw.exe")] + "python.exe"
            proc = subprocess.Popen([python, "-m", "hub_core.unattended", "respond",
                                     row["id"], "--workspace", workspace],
                                    cwd=str(Path(__file__).resolve().parents[2]),
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, creationflags=flags,
                                    start_new_session=(os.name != "nt"))
            found["launched"].append({"id": row["id"], "pid": proc.pid})
    return found


def status() -> dict:
    recent = sorted(board.runs().values(), key=lambda r: r.get("created", 0))[-12:]
    return {"agent": agent(), "machine": machine(), "runtime": runtime.selected(),
            "runtime_executable": runtime.executable() or None,
            "paused": disabled() or paused() or None,
            "disarmed": auth_expired() or None,
            "hourly_ceiling": {"all": hourly_ceiling(), "attention": attention_ceiling(),
                               "launched_last_hour": board.launches_in_last_hour()},
            "publisher": {"hosts": publisher.hosts(), "push": publisher.push_state()},
            "lanes": lanes.status(), "attempts": board.responses(), "recent_runs": recent}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hub_core.unattended",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--home", help="state directory (default HUB_UNATTENDED_HOME or "
                                       "~/.hub-unattended); its unattended.env is loaded")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--launch", action="store_true")
    s.add_argument("--max", type=int, default=3)
    s.add_argument("--workspace", default=os.getcwd())
    r = sub.add_parser("respond")
    r.add_argument("item_id")
    r.add_argument("--workspace", default=os.getcwd())
    r.add_argument("--repo", default="", help="clone URL or path of the task's repository")
    p = sub.add_parser("publish")
    p.add_argument("--workspace", default=os.getcwd())
    sub.add_parser("sweep")
    sub.add_parser("status")
    sub.add_parser("lanes")
    args = parser.parse_args(argv)
    if args.home:
        os.environ["HUB_UNATTENDED_HOME"] = args.home
    load_env_file(home() / "unattended.env")
    try:
        if args.command == "scan":
            result = scan(args.launch, args.max, args.workspace)
        elif args.command == "respond":
            result = respond(args.item_id.strip(), args.workspace, args.repo)
        elif args.command == "publish":
            result = publisher.publish(args.workspace)
        elif args.command == "sweep":
            result = {"swept": board.sweep_orphans()}
        elif args.command == "lanes":
            result = lanes.status()
        else:
            result = status()
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
