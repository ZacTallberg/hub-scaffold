"""AIMD adaptive WIP ceiling: the hub computes max_active_leases from its own event history.

Stack-neutral and pure (no I/O): the ceiling is FOLDED from the ledger, never stored as mutable
config, so every consumer (claim gate, next rail, cockpit) derives the same number from the same
events — single-fold discipline. Classic additive-increase / multiplicative-decrease:

  +1        per event carrying an all-green verification_run receipt, once the fleet has ASKED
            for more concurrency (a proven completion is evidence it can digest what it has);
  halve     (floor FLOOR) per congestion signal — a recorded failed receipt, meaning an event
            whose verification_run carries a non-zero exit_code.

DEMAND IS NOT CONGESTION, and getting that backwards is what this module was doing. A refused
claim (``wip.saturated``) says workers WANT more concurrency than the ceiling allows. It says
nothing whatever about whether the fleet could digest it — so it ARMS the probe and never halves.
Halving on it made the controller measure appetite instead of capability, and the loop was
self-amplifying: a lower ceiling produced more refusals, which lowered it further. Measured on
this board on 2026-08-05, one worker's retry loop emitted seven refusals in fifty-one seconds and
each one halved; the ceiling reached the floor with every receipt on the board green, and 23
consecutive green completions over the next five hours could not lift it off 2.

The only honest loss signal a work queue has is work that came back broken. That is the failed
receipt, and it is the one thing here that still backs the fleet off.

The write seam records those signals as log-only events on the ``<project>:wip:ceiling``
aggregate (``wip`` is not a materialized entity type, so they never fold into board state).
The gate lives at the HTTP seams: /hub/api/claim refuses NEW leases with 429 board_saturated
when live leases >= ceiling (renewals pass — holding your lease must never deadlock digging the
board out), and /hub/next.json reports 429 instead of handing out work it would refuse to lease.
"""

BASE = 8     # cold-start ceiling before any history: above the stock fleet size (5), so a fresh
             # board throttles only on EVIDENCE of failure, yet 3 halvings still reach the floor.
             # hub_app.wip_status honours HUB_WIP_BASE for fixtures that need more cold headroom.
FLOOR = 4    # OPERATOR RULING 2026-08-05: the fleet is never throttled below four live leases.
             # A floor of 1 made ASKING for work self-defeating: every refused claim records a
             # saturation signal and halves the ceiling, so idle seats politely polling for a slot
             # drove 4 -> 2 -> 1 and locked out the whole fleet, including seats that had just
             # drained. Congestion control must back off on evidence that concurrency HURT, never
             # on evidence that workers WANT work — and with the stock fleet at four or five seats,
             # a floor beneath the seat count can only ration a fleet the operator is paying to run
             # in parallel. Halving above this floor still works exactly as before.
WINDOW = 40  # the CONTROL WINDOW: how many recent signals the ceiling is computed from
SATURATED_TYPE = "wip.saturated"
RECEIPT_FAILED_TYPE = "receipt.failed"


def _codes(ev):
    """Exit codes carried by an event's verification_run receipts ([] when it carries none)."""
    runs = (ev.get("payload") or {}).get("verification_run") or []
    if isinstance(runs, dict):
        runs = [runs]
    return [r.get("exit_code") for r in runs if isinstance(r, dict)]


def _signals(events):
    """The events the controller reacts to, oldest first: demand, congestion and green receipts.
    Everything else is board traffic the ceiling has no opinion about.

    A RUN of consecutive refusals collapses to one signal. Retrying a refused claim is one worker
    wanting one thing, not a fleet-wide escalation, and the control WINDOW is finite: seven
    refusals in fifty-one seconds — which is what one retry loop actually produced here — would
    otherwise evict a sixth of the recent history that the ceiling is supposed to be reading."""
    out = []
    for ev in events:
        codes = _codes(ev)
        saturated = ev.get("type") == SATURATED_TYPE
        if not (saturated or codes):
            continue
        if saturated and out and out[-1].get("type") == SATURATED_TYPE:
            continue                        # same episode of demand, already counted
        out.append(ev)
    return out


def ceiling(events, base=BASE, floor=FLOOR, capacity=None, window=WINDOW):
    """Fold the AIMD ceiling from a BOUNDED window of recent control signals.

    Two properties the lifetime fold did not have, and the reason it drifted useless:

      RECENCY — only the last `window` signals count. Folding every green receipt ever recorded
      made the ceiling a monotone function of the board's AGE (a board with 180 completions
      carried a ceiling near 190), so a gate meant to throttle a struggling fleet could never
      engage again after a busy week. Concurrency control is about the recent past.

      EVIDENCE — a green receipt raises the ceiling only when the fleet was actually SATURATED
      since the last raise: growth needs proof that the extra concurrency was WANTED and then
      digested, not merely that work finished. So a fleet that keeps hitting its ceiling and
      draining cleanly climbs back one at a time, and a quiet fleet stays where it is.

    The two signals are not symmetric and must not be treated as one. Demand ARMS; only a failed
    receipt HALVES. See the module docstring for what conflating them did to this board.

    `capacity` clamps the result to what the fleet can really run (seats configured or observed);
    None means unknown, and an unknown capacity never lowers the ceiling. Pure and deterministic:
    the same events always fold to the same number, on every consumer."""
    c = base
    armed = False                 # workers have asked for more than the ceiling allows
    for ev in _signals(events)[-max(1, int(window)):]:
        codes = _codes(ev)
        if ev.get("type") == SATURATED_TYPE:
            armed = True                    # DEMAND: the probe is now live, the ceiling unmoved
        elif any(code != 0 for code in codes):
            c = max(floor, c // 2)          # the fleet handed back work that did not work
        elif codes and armed:
            c += 1
            armed = False
    if capacity is not None:
        c = max(floor, min(c, int(capacity)))
    return c


def status(events, active_leases, base=BASE, floor=FLOOR, capacity=None, window=WINDOW):
    """{ceiling, active, saturated} — the one shape every consumer of the gate reads."""
    c = ceiling(events, base=base, floor=floor, capacity=capacity, window=window)
    return {"ceiling": c, "active": int(active_leases), "saturated": int(active_leases) >= c}
