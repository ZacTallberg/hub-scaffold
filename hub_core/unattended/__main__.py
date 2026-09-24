"""python -m hub_core.unattended <scan|respond|sweep|status|lanes>

    scan [--launch] [--max N]     what an unattended responder here could take (and what it may
                                  not, with why); --launch spawns one `respond` per workable item
    respond <item-id>             one bounded session for exactly this task or question, then gone
    sweep                         hand back leases left by runs whose launcher died
    status                        lanes, pauses, attempt records and recent runs on this machine
    lanes                         the lane slots and how the long lane was sized

Environment: HUB_API_BASE, HUB_AGENT_TOKEN (or HUB_WRITE_TOKEN), HUB_AGENT_ID (the operator identity
if this launcher answers questions), HUB_MACHINE, HUB_WORKER_RUNTIME (auto|claude|codex),
HUB_UNATTENDED_LONG_SLOTS, HUB_REPO_URL_TEMPLATE, HUB_UNATTENDED_HOME. See
patterns/unattended-responder.md.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import agent, board, escalation, faults, forensics, home, lanes, log, machine, read_json
from . import runtime, worktree, write_json

TASK_BOUND_S = 5400        # build, ship, see it deployed: the long lane
SHORT_BOUND_S = 1500       # answer a question, clear one thing
LEASE_TTL_S = 900          # renewed every heartbeat while the run lives; lapses soon after it dies
QUEUE_RECHECK_S = 120      # a launcher waiting for a lane re-reads its task this often
SUSPEND_SLACK_S = 900      # fallback when no heartbeat measured the sleep
CHARTER = Path(__file__).resolve().parent / "RESPONDER.md"


def _bound(kind: str) -> int:
    name = "HUB_UNATTENDED_TASK_BOUND_S" if kind == "task" else "HUB_UNATTENDED_SHORT_BOUND_S"
    try:
        return max(60, int(os.environ.get(name) or 0)) if os.environ.get(name) else (
            TASK_BOUND_S if kind == "task" else SHORT_BOUND_S)
    except ValueError:
        return TASK_BOUND_S if kind == "task" else SHORT_BOUND_S


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
    if rc is None:
        detail += "\nwhat it was doing: " + (
            forensics.format_facts(forensics.killed_pass_facts(workspace, started))
            or forensics.no_evidence(workspace, started))
    return detail


def respond(item_id: str, workspace: str, repo_url: str = "") -> dict:
    """One bounded session for ONE item. Returns the run's record (also printed)."""
    swept = board.sweep_orphans()
    if swept:
        log("sweep: %s" % swept)
    reason = paused()
    if reason:
        log("respond: %s stays pending: %s" % (item_id, reason))
        return {"item": item_id, "outcome": "paused", "why": reason}

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
    if int(record.get("attempts") or 0) >= board.MAX_ATTEMPTS:
        log("respond: %s already attempted %d times here; left for a person" % (
            item_id, record["attempts"]))
        return {"item": item_id, "outcome": "attempts-exhausted"}

    item = board.resolve(item_id)
    if item and item.get("kind") == "unavailable":
        return {"item": item_id, "outcome": "unverified", "why": "the hub could not be read"}
    if item is None:
        state = "stood-down"
        if ":task:" in item_id and board.task_status(item_id) == "done":
            state = "done"
        records[item_id] = {**record, "state": state, "at": time.time()}
        board.save_responses(records)
        return {"item": item_id, "outcome": state, "why": "no longer offered here"}
    kind = item["kind"]
    if kind == "question":
        ok, why = escalation.workable(item)
        if not ok:
            log("respond: %s not taken: %s" % (item_id, why))
            return {"item": item_id, "outcome": "waiting", "why": why}
    elif item["handed_back"] >= board.TASK_MAX_RUNS:
        return {"item": item_id, "outcome": "left-for-a-person",
                "why": "handed back %d times across machines" % item["handed_back"]}

    bound = _bound(kind)
    run_id = board.new_run_id()
    leases_file = str(home() / (run_id + ".leases.jsonl"))
    board.record_run(run_id, item=item_id, kind=kind, lane=lanes.lane_for(kind), bound_s=bound,
                     state="queued", queued=time.time(), leases_file=leases_file,
                     attempt=int(record.get("attempts") or 0) + 1)
    lock = lanes.LaneLock(lanes.lane_for(kind))
    wait_until, recheck_at = time.time() + 2 * bound, time.time() + QUEUE_RECHECK_S
    while not lock.acquire():
        if kind == "task" and time.time() >= recheck_at:
            recheck_at = time.time() + QUEUE_RECHECK_S
            if board.resolve(item_id) is None:
                board.record_run(run_id, state="ended", outcome="stood-down", ended=time.time())
                return {"item": item_id, "outcome": "stood-down",
                        "why": "taken or closed while queued for a lane"}
        if time.time() >= wait_until:
            board.record_run(run_id, state="ended", outcome="lane-full", ended=time.time())
            return {"item": item_id, "outcome": "lane-full"}
        time.sleep(15)

    token, tree, started = "", {}, time.time()
    try:
        board.record_run(run_id, state="running", lane_slot=lock.path.name if lock.path else "")
        item = board.resolve(item_id)            # the wait may have been long: re-read it
        if not item or item.get("kind") == "unavailable":
            board.record_run(run_id, state="ended", outcome="stood-down", ended=time.time())
            return {"item": item_id, "outcome": "stood-down", "why": "changed while queued"}
        session_dir = workspace
        env_extra = {"HUB_RUN_ID": run_id, "HUB_RUN_KIND": kind, "HUB_RUN_ITEM": item_id,
                     "HUB_RUN_LEASES": leases_file,
                     "HUB_RESPONDER_HOP": escalation.next_hop(item),
                     "PYTHONPATH": os.pathsep.join(filter(None, [
                         str(Path(__file__).resolve().parents[2]), os.environ.get("PYTHONPATH")]))}
        if kind == "task":
            token, claim = board.claim(item_id, LEASE_TTL_S)
            if not token:
                board.record_run(run_id, state="ended", outcome="stood-down", ended=time.time(),
                                 why=str(claim)[:300])
                return {"item": item_id, "outcome": "stood-down",
                        "why": "the hub refused the claim: %s" % str(claim)[:200]}
            with open(leases_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"task": item_id, "token": token, "at": time.time()}) + "\n")
            env_extra["HUB_LEASE_TOKEN"] = token
            session_dir, tree = worktree.prepare(item_id, item.get("title") or "", workspace, repo_url)
            board.record_run(run_id, workspace=session_dir, worktree=tree.get("path", ""))
        exe = runtime.executable()
        if not exe:
            raise RuntimeError("the %s runtime is not installed on this machine" % runtime.selected())
        records = board.responses()
        records[item_id] = {"state": "running", "at": time.time(),
                            "attempts": int(record.get("attempts") or 0) + 1}
        board.save_responses(records)
        prompt = _prompt(item, bound, tree)

        def beat():
            lock.beat()
            if token:
                board.heartbeat(item_id, token, LEASE_TTL_S)

        log("respond: launching %s for %s (%s, bounded %ds, %s)" % (
            runtime.selected(), item_id, kind, bound, lock.path.name if lock.path else "?"))
        board.record_run(run_id, launched=time.time(), runtime=runtime.selected())
        started = time.time()
        rc, out, timing = runtime.run_bounded(
            runtime.command(prompt), workspace=session_dir, bound_s=bound,
            env=runtime.worker_env(env_extra), on_beat=beat)
        elapsed = time.time() - started
        verdict = faults.classify(rc, out)
        suspended = int(timing.get("suspended_s") or 0) if rc is None else 0
        if rc is None and not suspended and elapsed > bound + SUSPEND_SLACK_S:
            suspended = int(elapsed - bound)

        if kind == "task":
            result = board.outcome(board.task_status(item_id))
        else:
            still = board.resolve(item_id)
            result = "cleared" if still is None else "not-cleared"
        if result != "cleared" and rc is None:
            result = "suspended" if suspended else "timed-out"
        lane_fault = verdict["kind"] in ("usage-limit", "harness-dead", "api-error")
        if lane_fault and result != "cleared":
            result = "lane-fault:" + verdict["kind"]

        # Attempts: a lane fault is not a try on the item.
        records = board.responses()
        entry = records.get(item_id) or {"attempts": 1}
        entry.update({"at": time.time(), "tokens": verdict["tokens"], "rc": rc,
                      "state": "done" if result == "cleared" else result})
        if lane_fault:
            entry["attempts"] = max(0, int(entry.get("attempts") or 1) - 1)
        records[item_id] = entry
        board.save_responses(records)

        details = _details(item_id, kind, rc, verdict, elapsed, out, timing, session_dir, started)
        if verdict["kind"] == "usage-limit":
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
        # the token as proof — including the item's own when the run left it active.
        note = ("The unattended run holding this task ended (%s) with the task unfinished; handed "
                "back by its launcher. The steps above carry what it recorded."
                % result.replace("left-active", "its clock ran out" if rc is None else "it exited"))
        freed = board.release_left(leases_file, note, uncharged=lane_fault and result != "cleared")
        if kind == "task" and result == "left-active" and any(f["task"] == item_id for f in freed):
            result = "handed-back"
        dirty = _uncommitted(session_dir)
        settled = worktree.settle(tree, "cleared" if result == "cleared" else result) if tree else ""
        pruned = worktree.prune(workspace, item_id.rsplit(":task:", 1)[0], skip=tree.get("path", "")) \
            if kind == "task" else []
        run = board.record_run(run_id, state="ended", ended=time.time(), rc=rc, outcome=result,
                               session=verdict["session"], tokens=verdict["tokens"],
                               turns=verdict["turns"], timing=timing, suspended_s=suspended,
                               freed=freed, uncommitted=dirty, worktree_settled=settled,
                               pruned=pruned, fault=verdict["kind"] if lane_fault else "")
        if dirty and result != "cleared":
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
                           "item=%s\n%s" % (item_id, str(exc)[:1500]))
        freed = board.release_left(leases_file, "The unattended launcher failed before the run "
                                    "finished; handed back so the task is not held by nobody.")
        return board.record_run(run_id, state="ended", ended=time.time(), outcome="launcher-failed",
                                error="%s: %s" % (type(exc).__name__, str(exc)[:300]), freed=freed)
    finally:
        lock.release()


def scan(launch: bool, limit: int, workspace: str) -> dict:
    found = board.scan()
    found["paused"] = paused()
    found["launched"] = []
    if launch and not found["paused"]:
        for row in found["workable"][:max(0, limit)]:
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | runtime.NO_WINDOW if os.name == "nt" else 0
            proc = subprocess.Popen([sys.executable, "-m", "hub_core.unattended", "respond",
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
            "runtime_executable": runtime.executable() or None, "paused": paused() or None,
            "lanes": lanes.status(), "attempts": board.responses(), "recent_runs": recent}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hub_core.unattended",
                                     description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--launch", action="store_true")
    s.add_argument("--max", type=int, default=3)
    s.add_argument("--workspace", default=os.getcwd())
    r = sub.add_parser("respond")
    r.add_argument("item_id")
    r.add_argument("--workspace", default=os.getcwd())
    r.add_argument("--repo", default="", help="clone URL or path of the task's repository")
    sub.add_parser("sweep")
    sub.add_parser("status")
    sub.add_parser("lanes")
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            result = scan(args.launch, args.max, args.workspace)
        elif args.command == "respond":
            result = respond(args.item_id.strip(), args.workspace, args.repo)
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
