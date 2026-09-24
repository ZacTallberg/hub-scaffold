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


def _live(claims: dict, now: float, ttl_s: float) -> dict:
    return {k: v for k, v in claims.items()
            if isinstance(v, dict) and now - float(v.get("at") or 0) < ttl_s}


def live(hub_dir, now: float | None = None, ttl_s: float = TTL_S) -> dict:
    """``{item: {machine, agent, at, age_s, releases_in_s}}`` for every claim still in force.
    Read-only and fail-soft: an unreadable file is "no claims", which leaves items visible as
    unclaimed -- the safe direction."""
    now = time.time() if now is None else now
    out = {}
    for item, row in _live(_read(hub_dir), now, ttl_s).items():
        age = int(now - float(row.get("at") or now))
        out[item] = {"machine": row.get("machine"), "agent": row.get("agent"),
                     "at": row.get("at"), "age_s": age,
                     "releases_in_s": max(0, int(ttl_s - age))}
    return out


def claim(hub_dir, item: str, machine: str, agent: str = "", *, release: bool = False,
          now: float | None = None, ttl_s: float = TTL_S) -> tuple:
    """``(granted, row_or_holder)``. Atomic under a process-and-thread lock.

    release=True gives the claim back -- only the holding machine may."""
    item = str(item or "").strip()[:200]
    machine = str(machine or "").strip().lower()[:120]
    agent = str(agent or "").strip().lower()[:120]
    if not item or not machine:
        raise ValueError("item and machine are required")
    now = time.time() if now is None else now
    hub_dir = Path(hub_dir)
    hub_dir.mkdir(parents=True, exist_ok=True)
    with ProcessFileLock(hub_dir, name=".item-claims.lock", timeout=5):
        claims = _live(_read(hub_dir), now, ttl_s)
        held = claims.get(item)
        if held and held.get("machine") != machine:
            age = int(now - float(held.get("at") or now))
            return False, {"machine": held.get("machine"), "agent": held.get("agent"),
                           "age_s": age, "releases_in_s": max(0, int(ttl_s - age))}
        if release:
            claims.pop(item, None)
            row = {"item": item, "machine": machine, "released": bool(held)}
        else:
            # A re-claim by the same machine renews the TTL from now.
            claims[item] = {"machine": machine, "agent": agent, "at": now}
            row = {"item": item, "machine": machine, "agent": agent,
                   "renewed": bool(held), "ttl_s": int(ttl_s)}
        if len(claims) > MAX_CLAIMS:
            claims = dict(sorted(claims.items(),
                                 key=lambda kv: float(kv[1].get("at") or 0))[-MAX_CLAIMS:])
        tmp = _path(hub_dir).with_suffix(".tmp%d" % os.getpid())
        tmp.write_text(json.dumps(claims, sort_keys=True), encoding="utf-8")
        os.replace(tmp, _path(hub_dir))
    return True, row
