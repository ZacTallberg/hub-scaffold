"""Who is actually there: one authority for every surface that would otherwise infer it.

THE CLASS THIS REMOVES. A board keeps saying somebody is on something because a clock has not run
out: a lease renews itself under a console that closed hours ago, and a second console of the same
agent renews that lease in place, takes its fencing token, and closes work the first console is
still doing. In both cases the mechanism could not support the claim on the surface. A lease is
held by a LIVE CONSOLE; a clock is only the backstop for the case where liveness cannot be
established.

THE ROSTER IS EVIDENCE OF PRESENCE, NOT EVIDENCE OF ABSENCE. The naive test -- "the holder is not
on the roster, so it is gone" -- reads as obviously right and steals live work: presence only
carries a machine while that machine can reach the hub, so when a machine goes quiet (asleep, off
the network, saturated) ALL its consoles vanish from the roster at once, exactly when their work
is most likely real and in flight. One dropped heartbeat would free every claim on that machine.

SO THE TEST IS NOT MEMBERSHIP. It is: the roster is COMPLETE FOR THE HOLDER'S MACHINE, and the
holder is absent from that complete slice. Three answers, never two:

    LIVE        the console reported within the live window
    GONE        its machine IS reporting, and the console is not among that machine's live ones
    UNPROVABLE  presence could not be read, the holder's machine is silent, or the holder named
                no console at all -- absence proves nothing here

GONE, once it has lasted ``GONE_GRACE_S`` from the console's last-seen stamp, RELEASES what the
holder held: the lease sweep hands its task back, an item claim is free to take, and a renewal
from another console takes over the record. Inside the grace it releases nothing, and every
refusal says when it will. UNPROVABLE is never a reason to free, refuse or rewrite anything; the lease's own expiry is the
only thing allowed to act on an unprovable holder.

ENDED IS GONE AT ONCE. A console presence records as over -- its session state ``gone`` (it said
goodbye), or an unattended run its launcher stamped ``ended`` (or reported ``done``) -- is not inferred
from absence: it is a positive observation, whatever its machine is doing. A task held by a
console that is over has no owner, and a task without an owner is taken over immediately; the
grace exists only to protect a console that might merely have stepped away, which an ended one
cannot have. Ended sessions are never LIVE, even inside the live window. The window ordering is load-bearing:
REPORTING_WINDOW_S < presence.SESSION_ACTIVE_S, so by the time a console drops out of the live
set because its machine went quiet, that machine has been silent for several reporting windows
and the holder reads UNPROVABLE, not GONE.

A BEATING MACHINE THAT WENT SILENT IS THE ONE EXCEPTION. A machine whose row carries a heartbeat
stamp runs a daemon that beats every minute or so; when THAT machine has made no request at all
for ``MACHINE_SILENT_S``, its silence is no longer "maybe a dropped beat" but positive evidence
the machine is off, asleep or cut off -- and a lease it holds would otherwise lock the task until
its own clock ran out (on the origin system a responder's machine went quiet a minute after its
claim and the task stayed locked for more than three hours while the board already called the
lease abandoned). Such a holder is GONE with ``machine_silent`` and released at once. It is safe
because releasing never trusts the old holder again: the takeover rotates the fencing token, so
a machine that wakes up cannot complete over the new holder, and its next claim/step/finish is
told ``taken_over``. A machine with no heartbeat stamp (no daemon, only requests) never qualifies:
its quiet is not evidence of anything.

Framework-free and read-only: it reads the presence sidecar and never writes.
"""
from __future__ import annotations

import time

from . import presence as _presence

LIVE = "live"
GONE = "gone"
UNPROVABLE = "unprovable"

#: A machine that has reported anything (heartbeat or an authenticated write) this recently is
#: REPORTING: its console list is complete enough that a missing console is a closed one.
REPORTING_WINDOW_S = 300
#: How long a holder must have been GONE -- measured from its console's LAST-SEEN stamp, not from
#: when it claimed -- before what it held is released. Longer than the live window, so a console
#: that stepped away briefly is never robbed, and far shorter than a lease or claim TTL, so a
#: closed console's work is back on offer within the half hour instead of hours later.
GONE_GRACE_S = 1800
#: A lease claimed or renewed this recently is HELD even when its console is not on the roster
#: yet: a new console's first presence row can land minutes after its first claim, and a second
#: console of the same agent renewing in that gap would take the token of work that just began.
FRESH_LEASE_S = 600
#: A machine that HEARTBEATS (its row carries heartbeat_at) and has made no request of any kind
#: for this long is positively silent: what it holds is released (module docstring). Ten missed
#: beats at the default one-minute interval.
MACHINE_SILENT_S = 600


def _norm_session(value) -> str:
    return str(value or "").strip()[:64]


def _norm_machine(value) -> str:
    return str(value or "").strip().lower()[:120]


class Roster:
    """A resolved answer to "who is on this board right now", with its own completeness."""

    __slots__ = ("live", "last_seen", "machine_of", "reporting", "silent", "readable", "at",
                 "ended", "machine_last", "beating")

    def __init__(self, live=None, last_seen=None, machine_of=None, reporting=None,
                 silent=None, readable=True, at=None, ended=None, machine_last=None,
                 beating=None):
        self.ended = {_norm_session(s) for s in (ended or ()) if _norm_session(s)}
        self.live = {_norm_session(s) for s in (live or ()) if _norm_session(s)} - self.ended
        self.last_seen = dict(last_seen or {})
        self.machine_of = dict(machine_of or {})
        self.reporting = {_norm_machine(m) for m in (reporting or ()) if _norm_machine(m)}
        self.silent = {_norm_machine(m) for m in (silent or ()) if _norm_machine(m)}
        self.readable = bool(readable)
        self.at = float(at or time.time())
        self.machine_last = {_norm_machine(m): float(v or 0) for m, v in (machine_last or {}).items()
                             if _norm_machine(m)}
        self.beating = {_norm_machine(m) for m in (beating or ()) if _norm_machine(m)}

    def machine_silent_s(self, machine: str, now: float | None = None):
        """Seconds since a HEARTBEATING machine made any request, or None when that cannot be
        established (unreadable roster, unknown machine, or a machine that never heartbeats).
        None never releases anything: only positive evidence of a silent beacon does."""
        mach = _norm_machine(machine)
        if not self.readable or not mach or mach not in self.beating:
            return None
        last = self.machine_last.get(mach)
        if not last:
            return None
        return max(0.0, (time.time() if now is None else now) - last)

    @property
    def partial(self) -> bool:
        """True when a machine that has reported before is silent now: consoles on it may be
        working and cannot be seen from here."""
        return bool(self.silent)

    def state(self, session: str, machine: str = "") -> tuple:
        """``(LIVE | GONE | UNPROVABLE, last_seen_epoch_or_None)`` for one holder.

        `machine` is the machine the HOLDER recorded when it took the claim -- the claim is the
        one record that survives the holder's disappearance, so pass it. When it is missing the
        roster's own record of that console's machine is used; when neither knows, UNPROVABLE.
        """
        sid = _norm_session(session)
        seen = self.last_seen.get(sid)
        if not sid:
            return UNPROVABLE, None          # a board click or a bare script names no console
        if not self.readable:
            return UNPROVABLE, seen          # presence could not be read at all
        if sid in self.ended:
            return GONE, seen                # it said it is over: positive, not inferred
        if sid in self.live:
            return LIVE, seen
        mach = _norm_machine(machine) or self.machine_of.get(sid, "")
        if not mach:
            return UNPROVABLE, seen          # no machine whose slice could be shown complete
        if mach not in self.reporting:
            return UNPROVABLE, seen          # the MACHINE is quiet, not necessarily the console
        return GONE, seen

    def gone_for(self, session: str, machine: str = "", floor: float = 0.0,
                 now: float | None = None) -> tuple:
        """``(state, seconds_gone_or_None)``. The clock runs from the console's LAST-SEEN stamp,
        not from when the claim was taken; `floor` (the claim time) bounds it when presence has
        already pruned the console, so this is never more aggressive than the lease clock."""
        now = time.time() if now is None else now
        state, seen = self.state(session, machine)
        if state != GONE:
            return state, None
        since = max(float(seen or 0), float(floor or 0))
        return GONE, (max(0.0, now - since) if since else None)

    def is_ended(self, session: str) -> bool:
        """True when presence recorded this console as over (said goodbye, or a finished run)."""
        sid = _norm_session(session)
        return bool(sid) and sid in self.ended

    def as_dict(self) -> dict:
        return {"live": len(self.live), "ended": len(self.ended), "beating": sorted(self.beating),
                "reporting": sorted(self.reporting),
                "silent": sorted(self.silent), "partial": self.partial,
                "readable": self.readable}


def resolve(hub_dir, now: float | None = None) -> Roster:
    """Read presence once and answer who is live, which machines are reporting, and when every
    console presence still carries was last seen.

    FAILS CLOSED: any failure to read or flatten presence produces an UNREADABLE roster, under
    which every holder is UNPROVABLE and nothing is ever freed or refused. An empty set on error
    would make every session-bearing lease look gone at once."""
    now = time.time() if now is None else now
    try:
        live, last_seen, machine_of, reporting, silent, ended = set(), {}, {}, set(), set(), set()
        machine_last, beating = {}, set()
        for row in _presence.rows(hub_dir):
            machine = _norm_machine(row.get("machine"))
            stamp = max(_presence.epoch(row.get("heartbeat_at")),
                        _presence.epoch(row.get("activity_at")),
                        _presence.epoch(row.get("last_seen")))
            if machine:
                (reporting if stamp and now - stamp <= REPORTING_WINDOW_S else silent).add(machine)
                machine_last[machine] = max(machine_last.get(machine, 0.0), stamp)
                if _presence.epoch(row.get("heartbeat_at")):
                    beating.add(machine)
            sessions = row.get("sessions") if isinstance(row.get("sessions"), dict) else {}
            for sid, data in sessions.items():
                sid = _norm_session(sid)
                if not sid or not isinstance(data, dict):
                    continue
                at = _presence.epoch(data.get("at"))
                if at >= last_seen.get(sid, 0.0):
                    last_seen[sid] = at
                    if machine:
                        machine_of[sid] = machine
                unattended = str(data.get("kind") or "attended") != "attended"
                if (str(data.get("state") or "") == "gone"
                        or (unattended and (str(data.get("state") or "") == "done"
                                            or _presence.epoch(data.get("ended"))))):
                    ended.add(sid)
                if at and now - at <= _presence.SESSION_ACTIVE_S:
                    live.add(sid)
        silent -= reporting        # a machine with one fresh row is reporting, whatever else
    except Exception:                                        # noqa: BLE001 - never break a read
        return Roster(readable=False, at=now)
    return Roster(live=live, last_seen=last_seen, machine_of=machine_of,
                  reporting=reporting, silent=silent, readable=True, at=now, ended=ended,
                  machine_last=machine_last, beating=beating)


def verdict(roster, session: str, machine: str = "", floor: float = 0.0,
            now: float | None = None, grace_s: float = GONE_GRACE_S) -> dict:
    """ONE answer to "may this holder's claim be released?", shared by every claim kind.

    ``{state, gone_s, frees_in_s, released}``: `released` is True only for a GONE holder whose
    grace has run out. LIVE and UNPROVABLE never release (a missing roster is UNPROVABLE)."""
    if roster is None:
        return {"state": UNPROVABLE, "gone_s": None, "frees_in_s": None, "released": False}
    if roster.is_ended(session):
        # Over, not stepped away: released at once, with no grace (module docstring).
        _state, gone_s = roster.gone_for(session, machine, floor=floor, now=now)
        return {"state": GONE, "gone_s": int(gone_s) if gone_s is not None else None,
                "frees_in_s": 0, "released": True, "ended": True}
    state, gone_s = roster.gone_for(session, machine, floor=floor, now=now)
    if state == UNPROVABLE:
        # A beating machine silent past MACHINE_SILENT_S: positive evidence, released at once
        # (module docstring). The console itself cannot be seen, so its machine speaks for it.
        mach = _norm_machine(machine) or roster.machine_of.get(_norm_session(session), "")
        quiet = roster.machine_silent_s(mach, now)
        if quiet is not None and quiet >= MACHINE_SILENT_S:
            return {"state": GONE, "gone_s": int(quiet), "frees_in_s": 0, "released": True,
                    "machine_silent": True}
    if state != GONE or gone_s is None:
        return {"state": state, "gone_s": None, "frees_in_s": None, "released": False}
    left = max(0, int(grace_s - gone_s))
    return {"state": GONE, "gone_s": int(gone_s), "frees_in_s": left, "released": left <= 0}


def holder(roster: Roster, lease: dict, now: float | None = None,
           grace_s: float = GONE_GRACE_S) -> dict:
    """The holder of one lease, as every surface should render it -- including, for a GONE
    holder, how long until what it holds frees itself."""
    lease = lease or {}
    v = verdict(roster, lease.get("session"), lease.get("machine"),
                floor=float(lease.get("last_heartbeat") or lease.get("claimed") or 0),
                now=now, grace_s=grace_s)
    return {"holder_session": _norm_session(lease.get("session")) or None,
            "holder_machine": _norm_machine(lease.get("machine")) or None,
            "holder_state": v["state"],
            "holder_gone_s": v["gone_s"],
            "holder_frees_in_s": v["frees_in_s"]}
