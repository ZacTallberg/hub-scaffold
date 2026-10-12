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

A CLAIM IS ON A THING, NOT ON A KEY. Several item ids name one piece of work: an attention
condition ABOUT a task (stalled, needs a person, an orphaned lease) is that task, and a condition
that clears and returns is a new id for the same condition. Each key therefore belongs to an
AGGREGATE (``aggregate_of``), and a claim is refused while ANOTHER machine holds any key of the same
aggregate. On the instance this was lifted from, a task ran on one machine while the attention
item about the same task ran on another in the same minute.

THE CLASS REMEMBERS. A recurrence is a new item id, so nothing remembered what the last session on
the same condition concluded, and the next machine started cold. A claim may name a ``cls`` (the
launcher sends the aggregate); the release that ends a run records its ending there (outcome,
whether it spent anything, its final message), and the next claim on the class returns that
history so the next session is told what the last one found. Bounded: ``HISTORY_KEEP_PER_CLASS``
endings per class, ``HISTORY_MAX_CLASSES`` classes, ``HISTORY_KEEP_S``.

Standard library only; the adapter supplies the directory and the auth.
"""
from __future__ import annotations

import json
import os
import re
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
HISTORY_FILE = "item-claim-history.json"
HISTORY_KEEP_PER_CLASS = 8
HISTORY_MAX_CLASSES = 1500
HISTORY_KEEP_S = 14 * 86400
HISTORY_FINAL_MAX = 2000

_TASK_IN = re.compile(r"([a-z0-9._-]+:task:[A-Za-z0-9._-]+)")


def aggregate_of(item: str) -> str:
    """The thing a claim is ON, whatever key it was taken under.

    ``class:<x>`` -> x; ``attention:<kind>:<subject>[:<since>][:escalated]`` -> the TASK when the
    subject names one (a condition about a task is that task), else ``attention:<kind>:<subject>``;
    ``problem:<pid>[:...]`` -> ``problem:<pid>`` and ``error:<fp>[:...]`` -> ``error:<fp>`` (a
    reopened or escalated spelling is the same problem); anything else is its own aggregate (a
    task id, a question id)."""
    i = str(item or "").strip()
    if i.startswith("class:"):
        i = i[len("class:"):]
    for head in ("problem:", "error:"):
        if i.startswith(head):
            return head + i[len(head):].split(":", 1)[0]
    if i.startswith("attention:"):
        rest = i[len("attention:"):]
        for suffix in (":escalated", ":leader"):
            if rest.endswith(suffix):
                rest = rest[:-len(suffix)]
        head, _, tail = rest.rpartition(":")
        rest = head if head and tail.isdigit() else rest
        m = _TASK_IN.search(rest.partition(":")[2])
        return m.group(1) if m else "attention:" + rest
    return i


def _history_path(hub_dir) -> Path:
    return Path(hub_dir) / HISTORY_FILE


def _read_history(hub_dir) -> dict:
    try:
        data = json.loads(_history_path(hub_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def history_for(hub_dir, cls: str, now: float | None = None) -> list:
    """The kept endings of earlier runs on this class, oldest first ([] for none)."""
    now = time.time() if now is None else now
    rows = _read_history(hub_dir).get(str(cls or "")) or []
    return [r for r in rows if isinstance(r, dict) and now - float(r.get("at") or 0) < HISTORY_KEEP_S]


def _record_ending(hub_dir, cls: str, ending: dict, now: float) -> None:
    """Append one run's ending to its class (caller holds the claims lock)."""
    hist = {k: v for k, v in _read_history(hub_dir).items()
            if isinstance(v, list) and any(isinstance(r, dict)
                                           and now - float(r.get("at") or 0) < HISTORY_KEEP_S
                                           for r in v)}
    try:
        tokens = max(0, int(ending.get("tokens") or 0))
    except (TypeError, ValueError):
        tokens = 0
    hist.setdefault(cls, []).append({
        "at": now, "machine": str(ending.get("machine") or "")[:120],
        "agent": str(ending.get("agent") or "")[:120], "item": str(ending.get("item") or "")[:200],
        "outcome": str(ending.get("outcome") or "")[:40], "tokens": tokens,
        "spent": bool(ending.get("spent")),
        "final": str(ending.get("final") or "")[-HISTORY_FINAL_MAX:]})
    hist[cls] = hist[cls][-HISTORY_KEEP_PER_CLASS:]
    if len(hist) > HISTORY_MAX_CLASSES:
        hist = dict(sorted(hist.items(), key=lambda kv: float(kv[1][-1].get("at") or 0))
                    [-HISTORY_MAX_CLASSES:])
    tmp = _history_path(hub_dir).with_suffix(".tmp%d" % os.getpid())
    tmp.write_text(json.dumps(hist, sort_keys=True), encoding="utf-8")
    os.replace(tmp, _history_path(hub_dir))


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


class StaleEpoch(ValueError):
    """The caller's ``expected_epoch`` is not the aggregate's current epoch: somebody claimed it
    since the caller looked."""

    def __init__(self, aggregate: str, expected: int, current: int):
        super().__init__("%s is at epoch %d, not %d: somebody claimed it since you looked"
                         % (aggregate, current, expected))
        self.aggregate, self.expected, self.current = aggregate, expected, current


def claim(hub_dir, item: str, machine: str, agent: str = "", *, release: bool = False,
          session: str = "", roster=None, now: float | None = None, ttl_s: float = TTL_S,
          grace_s: float | None = None, cls: str = "", ending: dict | None = None,
          expected_epoch=None) -> tuple:
    """``(granted, row_or_holder)``. Atomic under a process-and-thread lock.

    release=True gives the claim back -- only the holding machine may. `roster` (a
    hub_core.liveness.Roster) lets a claim whose console is GONE past its grace be taken over;
    without one, only the TTL frees a claim. A claim held on ANOTHER key of the same aggregate by
    another machine refuses this one. With `cls`, a grant or a refusal carries ``history`` (what
    earlier runs on the class concluded), and a release carrying `ending` ({outcome, final,
    tokens, spent}) records this run's ending there. A grant that makes a NEW holder of the
    aggregate bumps its fencing epoch (returned as ``epoch``; a same-machine refresh keeps it);
    ``expected_epoch`` makes that a compare-and-set (``StaleEpoch``). Every refusal is an event."""
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
    cls = str(cls or "").strip()[:200]
    with ProcessFileLock(hub_dir, name=".item-claims.lock", timeout=5):
        claims = _live(_read(hub_dir), now, ttl_s)
        held = claims.get(item)
        if not release and not (held and held.get("machine") != machine):
            # Any OTHER key of the same aggregate that another machine holds holds this one too.
            agg = aggregate_of(item)
            for key, row in claims.items():
                if key != item and row.get("machine") != machine and aggregate_of(key) == agg:
                    v = _judge(row, roster, now, grace_s)
                    if not v["released"]:
                        age = int(now - float(row.get("at") or now))
                        out = {"machine": row.get("machine"), "session": row.get("session"),
                               "agent": row.get("agent"), "age_s": age, "held_as": key,
                               "aggregate": agg,
                               "releases_in_s": max(0, int(_row_ttl(row, ttl_s) - age)),
                               "holder_state": v["state"], "gone_s": v["gone_s"],
                               "frees_in_s": v["frees_in_s"]}
                        if cls:
                            out["history"] = history_for(hub_dir, cls, now)
                        out["epoch"] = current_epoch(hub_dir, agg)
                        fence_event(hub_dir, "lost_claim", aggregate=agg, item=item,
                                    machine=machine, holder=row.get("machine"), held_as=key)
                        return False, out
        took_over = None
        if held and held.get("machine") != machine:
            v = _judge(held, roster, now, grace_s)
            age = int(now - float(held.get("at") or now))
            if not v["released"]:
                out = {"machine": held.get("machine"), "session": held.get("session"),
                       "agent": held.get("agent"), "age_s": age,
                       "releases_in_s": max(0, int(_row_ttl(held, ttl_s) - age)),
                       "holder_state": v["state"], "gone_s": v["gone_s"],
                       "frees_in_s": v["frees_in_s"]}
                if cls:
                    out["history"] = history_for(hub_dir, cls, now)
                if not release:
                    agg = aggregate_of(item)
                    out.update(aggregate=agg, epoch=current_epoch(hub_dir, agg))
                    fence_event(hub_dir, "lost_claim", aggregate=agg, item=item,
                                machine=machine, holder=held.get("machine"))
                return False, out
            took_over = {"machine": held.get("machine"), "session": held.get("session"),
                         "agent": held.get("agent"), "gone_s": v["gone_s"]}
            held = None
        if release:
            claims.pop(item, None)
            row = {"item": item, "machine": machine, "released": bool(held or took_over)}
            if cls and isinstance(ending, dict) and str(ending.get("outcome") or "").strip():
                _record_ending(hub_dir, cls, dict(ending, machine=machine, agent=agent,
                                                  item=item), now)
                row["recorded"] = cls
        else:
            # A re-claim by the same machine renews the TTL from now (and records the console
            # that renewed it, so liveness judges the console actually doing the work).
            granted_ttl = ttl_for(item, ttl_s)
            agg = aggregate_of(item)
            cur = current_epoch(hub_dir, agg)
            if expected_epoch is not None and int(expected_epoch) != cur:
                fence_event(hub_dir, "lost_cas", aggregate=agg, item=item, machine=machine,
                            expected=int(expected_epoch), current=cur)
                raise StaleEpoch(agg, int(expected_epoch), cur)
            # A same-machine refresh keeps its token; a NEW holder gets the next one.
            mine = [int(r.get("epoch") or 0) for k, r in claims.items()
                    if r.get("machine") == machine and aggregate_of(k) == agg]
            epoch = max(mine or [0])
            if not mine or not epoch or epoch < cur:
                epoch = bump_epoch(hub_dir, agg, "%s@%s" % (agent or "?", machine), now)
            entry = {"machine": machine, "agent": agent, "at": now, "ttl_s": granted_ttl,
                     "epoch": epoch}
            if session:
                entry["session"] = session
            elif held and held.get("session"):
                entry["session"] = held["session"]
            claims[item] = entry
            row = {"item": item, "machine": machine, "agent": agent,
                   "session": entry.get("session"), "renewed": bool(held), "ttl_s": granted_ttl}
            if took_over:
                row["took_over_from"] = took_over
            row["aggregate"] = agg
            row["epoch"] = epoch
            if cls:
                row["history"] = history_for(hub_dir, cls, now)
        if len(claims) > MAX_CLAIMS:
            claims = dict(sorted(claims.items(),
                                 key=lambda kv: float(kv[1].get("at") or 0))[-MAX_CLAIMS:])
        tmp = _path(hub_dir).with_suffix(".tmp%d" % os.getpid())
        tmp.write_text(json.dumps(claims, sort_keys=True), encoding="utf-8")
        os.replace(tmp, _path(hub_dir))
    return True, row


# ---------------------------------------------------------------- fencing epochs (one holder per thing)
#
# A claim per ITEM KEY cannot stop two machines working one thing: the launcher spells the same
# condition several ways (`attention:<kind>:<subject>:<since>`, the same with `:escalated`, a
# class key beside the item), and on the instance this was lifted from one problem was worked on
# one machine while another waited on the other spelling of it. So:
#
# * every claim belongs to an AGGREGATE (``aggregate_of``): the thing it is ON, whatever key it
#   was taken under. A claim is refused while ANOTHER machine holds any key of the same aggregate;
# * each aggregate has a monotonically increasing EPOCH. A claim that makes a NEW holder bumps it
#   (a compare-and-set when the caller sends ``expected_epoch``); a same-machine refresh keeps it;
# * a holder's WRITE that carries the epoch it was granted (``X-Hub-Claim-Aggregate`` +
#   ``X-Hub-Claim-Epoch``, sent by the client from HUB_CLAIM_AGGREGATE / HUB_CLAIM_EPOCH, which the
#   unattended launcher sets for the session it spawns) is refused ``409 stale_epoch`` once a newer
#   holder has claimed since. A write without the headers (an attended console) is not fenced.
#
# Every refusal is an EVENT (``claim-fence-events.jsonl``), never silent. (Fencing tokens in the
# sense of Kleppmann: a paused holder that wakes up cannot overwrite its successor.)

EPOCHS_FILE = "claim-epochs.json"
FENCE_EVENTS_FILE = "claim-fence-events.jsonl"
MAX_EPOCHS = 5000


def _epochs(hub_dir) -> dict:
    try:
        data = json.loads((Path(hub_dir) / EPOCHS_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def current_epoch(hub_dir, aggregate: str) -> int:
    try:
        return int((_epochs(hub_dir).get(aggregate) or {}).get("epoch") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def bump_epoch(hub_dir, aggregate: str, holder: str = "", now: float | None = None) -> int:
    """Increment and return the aggregate's epoch -- the new holder's fencing token. Never
    decreases; bounded to the newest ``MAX_EPOCHS`` aggregates."""
    from . import atomic
    now = time.time() if now is None else now
    hub_dir = Path(hub_dir)
    hub_dir.mkdir(parents=True, exist_ok=True)
    with ProcessFileLock(hub_dir, name=".claim-epochs.lock", timeout=5):
        rows = _epochs(hub_dir)
        cur = rows.get(aggregate) if isinstance(rows.get(aggregate), dict) else {}
        n = int(cur.get("epoch") or 0) + 1
        rows[aggregate] = {"epoch": n, "at": now, "holder": str(holder or "")[:120]}
        if len(rows) > MAX_EPOCHS:
            rows = dict(sorted(rows.items(),
                               key=lambda kv: float((kv[1] or {}).get("at") or 0))[-MAX_EPOCHS:])
        atomic.write_json(hub_dir / EPOCHS_FILE, rows)
    return n


def fence_event(hub_dir, kind: str, **fields) -> None:
    """One line per lost claim, lost compare-and-set or refused stale write. Never raises."""
    try:
        with open(Path(hub_dir) / FENCE_EVENTS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict({"at": time.time(), "kind": kind}, **fields)) + "\n")
    except OSError:
        pass


def check_epoch(hub_dir, carried_aggregate: str, carried_epoch, target: str, **context):
    """None when the write may proceed; else the refusal (a dict for a 409 ``stale_epoch``) when
    the writer carries an epoch for THIS target's aggregate that a newer claim has superseded. No
    epoch, or an epoch for a different aggregate, is not fenced: the epoch a session carries is
    for the item it was launched for, and elsewhere it writes as any console does."""
    agg = aggregate_of(carried_aggregate or "")
    raw = str(carried_epoch or "").strip()
    if not raw or not agg or agg != aggregate_of(target):
        return None
    try:
        carried = int(raw)
    except ValueError:
        return None
    cur = current_epoch(hub_dir, agg)
    if carried >= cur:
        return None
    fence_event(hub_dir, "stale_write", aggregate=agg, carried=carried, current=cur, **context)
    return {"code": "stale_epoch",
            "msg": "your claim on %s is epoch %d, but it was claimed again since (epoch %d): "
                   "another holder owns it now, so this write is refused. Stop, and do not retry "
                   "it." % (agg, carried, cur),
            "aggregate": agg, "epoch": cur, "carried": carried}
