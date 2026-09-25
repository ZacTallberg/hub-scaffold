"""One responder per item, across every machine: an atomic TTL claim on the Hub.

A TASK already has this -- its fenced lease. Other work does not: an unclaimed operational
error, or a question addressed to the operator, is visible to EVERY machine's launcher at once,
and one person commonly runs several machines. Without a shared claim, two machines each spend
a session fixing the same thing inside one wait cycle: duplicate work at best, conflicting pushes
at worst. A per-machine spawn ledger cannot see the other machine.

So an item gets a claim here. The first machine is granted; a DIFFERENT machine is refused while
the claim lives, and the refusal names the holder and when the claim releases; the SAME machine
re-claiming is granted idempotently (its own retry). Claims expire on their own (``TTL_S``), so a
claimant that died without cleaning up releases by timeout instead of stranding the item forever.
Expired rows are swept and the file is bounded on every write.

A CLAIM IS HELD BY A CONSOLE, not only by a clock. The claim records the console (session) that
took it. When the Hub can PROVE that console is gone -- its machine is reporting and the console is
not among its live ones (hub_core.liveness) -- for longer than ``liveness.GONE_GRACE_S`` from its
last-seen stamp, the claim is released: ``live`` stops reporting it and ``claim`` grants the item
to the next machine, recording whom it took over from. Inside the grace the refusal says so and
says when it frees itself. An UNPROVABLE holder (quiet machine, unreadable presence, no console
recorded) releases only by the TTL.

A held item is WAITING, not unclaimed: every surface that counts "nobody has looked at this"
(the attention rail, the error bar's unclaimed count) reads ``live`` and reports a claimed item
as in flight, naming the machine -- a false "unclaimed" sends a second agent to dig at work that
is already held, and teaches the reader that the alarm is decoration.

Standard library only; the adapter supplies the directory and the auth.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .process_lock import ProcessFileLock

#: Twice a bounded responder run: long enough to outlive a slow run, short enough that a dead
#: claimant's item is back on offer the same hour.
TTL_S = 1800
#: A TASK run is bounded far longer (build, push, wait for the deploy: hub_core.responder
#: TASK_BOUND_S), so its claim lives twice THAT, by the same rule -- at the error/ask TTL a second
#: machine could take a task a live run was still working. The TTL rides the row.
TASK_TTL_S = 10800


def ttl_for(item: str, default: float = TTL_S) -> int:
    """The claim's lifetime by the kind of item: a board task, or anything else."""
    return int(TASK_TTL_S if ":task:" in str(item or "") else default)
MAX_CLAIMS = 500
FILE = "item-claims.json"


def _path(hub_dir) -> Path:
    return Path(hub_dir) / FILE


def _read(hub_dir) -> dict:
    try:
        data = json.loads(_path(hub_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _row_ttl(v: dict, default: float) -> float:
    try:
        return float(v.get("ttl_s") or default)
    except (TypeError, ValueError):
        return float(default)


def _live(claims: dict, now: float, ttl_s: float) -> dict:
    """Claims still in force. Each row carries the TTL it was granted with (a task's is longer);
    a row written before that field existed uses `ttl_s`."""
    return {k: v for k, v in claims.items()
            if isinstance(v, dict) and now - float(v.get("at") or 0) < _row_ttl(v, ttl_s)}


def _judge(row: dict, roster, now: float, grace_s: float) -> dict:
    from . import liveness
    return liveness.verdict(roster, row.get("session"), row.get("machine"),
                            floor=float(row.get("at") or 0), now=now, grace_s=grace_s)


def _grace():
    from . import liveness
    return liveness.GONE_GRACE_S


def live(hub_dir, now: float | None = None, ttl_s: float = TTL_S, roster=None,
         grace_s: float | None = None) -> dict:
    """``{item: {machine, session, agent, at, age_s, releases_in_s, holder_state, gone_s,
    frees_in_s}}`` for every claim still in force. A claim whose console is GONE past its grace
    is NOT in force and is omitted. Read-only and fail-soft: an unreadable file is "no claims",
    which leaves items visible as unclaimed -- the safe direction."""
    now = time.time() if now is None else now
    grace_s = _grace() if grace_s is None else grace_s
    out = {}
    for item, row in _live(_read(hub_dir), now, ttl_s).items():
        v = _judge(row, roster, now, grace_s)
        if v["released"]:
            continue
        age = int(now - float(row.get("at") or now))
        out[item] = {"machine": row.get("machine"), "session": row.get("session") or None,
                     "agent": row.get("agent"), "at": row.get("at"), "age_s": age,
                     "ttl_s": int(_row_ttl(row, ttl_s)),
                     "releases_in_s": max(0, int(_row_ttl(row, ttl_s) - age)),
                     "holder_state": v["state"], "gone_s": v["gone_s"],
                     "frees_in_s": v["frees_in_s"]}
    return out


def claim(hub_dir, item: str, machine: str, agent: str = "", *, release: bool = False,
          session: str = "", roster=None, now: float | None = None, ttl_s: float = TTL_S,
          grace_s: float | None = None) -> tuple:
    """``(granted, row_or_holder)``. Atomic under a process-and-thread lock.

    release=True gives the claim back -- only the holding machine may. `roster` (a
    hub_core.liveness.Roster) lets a claim whose console is GONE past its grace be taken over;
    without one, only the TTL frees a claim."""
    item = str(item or "").strip()[:200]
    machine = str(machine or "").strip().lower()[:120]
    agent = str(agent or "").strip().lower()[:120]
    session = str(session or "").strip()[:64]
    if not item or not machine:
        raise ValueError("item and machine are required")
    now = time.time() if now is None else now
    grace_s = _grace() if grace_s is None else grace_s
    hub_dir = Path(hub_dir)
    hub_dir.mkdir(parents=True, exist_ok=True)
    with ProcessFileLock(hub_dir, name=".item-claims.lock", timeout=5):
        claims = _live(_read(hub_dir), now, ttl_s)
        held = claims.get(item)
        took_over = None
        if held and held.get("machine") != machine:
            v = _judge(held, roster, now, grace_s)
            age = int(now - float(held.get("at") or now))
            if not v["released"]:
                return False, {"machine": held.get("machine"), "session": held.get("session"),
                               "agent": held.get("agent"), "age_s": age,
                               "releases_in_s": max(0, int(_row_ttl(held, ttl_s) - age)),
                               "holder_state": v["state"], "gone_s": v["gone_s"],
                               "frees_in_s": v["frees_in_s"]}
            took_over = {"machine": held.get("machine"), "session": held.get("session"),
                         "agent": held.get("agent"), "gone_s": v["gone_s"]}
            held = None
        if release:
            claims.pop(item, None)
            row = {"item": item, "machine": machine, "released": bool(held or took_over)}
        else:
            # A re-claim by the same machine renews the TTL from now (and records the console
            # that renewed it, so liveness judges the console actually doing the work).
            granted_ttl = ttl_for(item, ttl_s)
            entry = {"machine": machine, "agent": agent, "at": now, "ttl_s": granted_ttl}
            if session:
                entry["session"] = session
            elif held and held.get("session"):
                entry["session"] = held["session"]
            claims[item] = entry
            row = {"item": item, "machine": machine, "agent": agent,
                   "session": entry.get("session"), "renewed": bool(held), "ttl_s": granted_ttl}
            if took_over:
                row["took_over_from"] = took_over
        if len(claims) > MAX_CLAIMS:
            claims = dict(sorted(claims.items(),
                                 key=lambda kv: float(kv[1].get("at") or 0))[-MAX_CLAIMS:])
        tmp = _path(hub_dir).with_suffix(".tmp%d" % os.getpid())
        tmp.write_text(json.dumps(claims, sort_keys=True), encoding="utf-8")
        os.replace(tmp, _path(hub_dir))
    return True, row
