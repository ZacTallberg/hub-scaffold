#!/usr/bin/env python3
"""Is the fleet still COMPLETING TASKS? Not: is it still running.

A seat that is alive proves nothing. It can hold a lease, renew it forever, re-read the charter
every cycle and finish nothing, and every liveness signal there is - a pid, a heartbeat file, an
open window, a fresh telemetry row - reads green the entire time. `is-active` is not `is-working`.
The only honest evidence a seat is working is a `done` transition it earned with a receipt, on the
canonical ledger, which it cannot write without passing the receipt gate.

So health here is measured in COMPLETIONS, from the ledger, per seat:

  conversion  done / claims   - a seat claiming far more than it finishes is thrashing, not working
  staleness   minutes since its last done - a seat that has stopped finishing, whatever it looks like

The difference is not theoretical. On the board this was built for, one seat converted 43 of 45
claims (96%) while two others converted 31 of 153 (20%) and 17 of 97 (18%) - all three alive and
claiming the whole time, and a process check called all three green.

  python tools/seat_productivity.py                 # report every seat, newest activity first
  python tools/seat_productivity.py --agent <id>    # one seat (what a worker asks about itself)
  python tools/seat_productivity.py --stale-min 45  # exit 3 if NO seat has completed in that window

Exit: 0 = the fleet is completing work; 3 = nobody has completed anything inside the window
(a fleet-level alarm, not a seat-level one); 2 = usage error.
"""
import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLAIM_STATUSES = ("active",)


def _hub_dir():
    return Path(os.environ.get("HUB_DIR") or (ROOT / "PROJECT" / ".hub"))


def _parse_ts(raw):
    try:
        return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def read_seats(hub=None):
    """Per-seat {done, claims, last_done, last_seen} folded from the ledger.

    The ledger is the ONLY source here on purpose: a seat cannot inflate its own numbers without
    passing the write gate, so this measurement is not something a broken worker can fake.
    """
    hub = Path(hub) if hub else _hub_dir()
    ledger = hub / "events.jsonl"
    seats = {}
    if not ledger.exists():
        return seats
    for line in ledger.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        payload = ev.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        agent = ev.get("agent_id") or payload.get("agent")
        if not agent:
            continue
        row = seats.setdefault(agent, {"done": 0, "claims": 0, "last_done": None, "last_seen": None})
        ts = ev.get("ts")
        row["last_seen"] = ts or row["last_seen"]
        status = payload.get("status")
        if status == "done":
            row["done"] += 1
            row["last_done"] = ts or row["last_done"]
        elif status in CLAIM_STATUSES:
            row["claims"] += 1
    return seats


def conversion(row):
    """done / claims. None when the seat has never claimed — unknown, never a score of zero."""
    return None if not row["claims"] else row["done"] / float(row["claims"])


def stale_minutes(row, now=None):
    """Minutes since this seat last COMPLETED something. None if it never has."""
    when = _parse_ts(row.get("last_done"))
    if when is None:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now - when).total_seconds() / 60.0


def fleet_stale_minutes(seats, now=None):
    """Minutes since ANY seat completed anything — the fleet-level question."""
    mins = [m for m in (stale_minutes(r, now) for r in seats.values()) if m is not None]
    return min(mins) if mins else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent")
    ap.add_argument("--stale-min", type=float, default=None,
                    help="exit 3 if no seat has completed a task within this many minutes")
    ap.add_argument("--hub-dir")
    ap.add_argument("--done-count", action="store_true",
                    help="print ONLY this agent's completion count (requires --agent). The seat's "
                         "own loop samples this before and after a cycle: unchanged means the "
                         "cycle produced nothing, which is the signal that drives escalation. A "
                         "count, not a self-report — it comes from the ledger the seat cannot forge.")
    args = ap.parse_args(argv)

    seats = read_seats(args.hub_dir)
    if args.done_count:
        if not args.agent:
            print("--done-count requires --agent", file=sys.stderr)
            return 2
        print((seats.get(args.agent) or {}).get("done", 0))
        return 0
    if not seats:
        print("no seats on this ledger yet — nothing has been claimed or completed")
        return 0

    rows = sorted(seats.items(), key=lambda kv: (kv[1].get("last_done") or "", kv[1]["done"]),
                  reverse=True)
    if args.agent:
        rows = [(a, r) for a, r in rows if a == args.agent]
        if not rows:
            print(f"{args.agent}: no ledger activity — this seat has never claimed or completed")
            return 0

    print("%-34s %6s %7s %6s  %s" % ("SEAT", "DONE", "CLAIMS", "CONV", "SINCE LAST DONE"))
    for agent, row in rows[:20]:
        conv = conversion(row)
        stale = stale_minutes(row)
        print("%-34s %6d %7d %6s  %s" % (
            str(agent)[:34], row["done"], row["claims"],
            "n/a" if conv is None else "%d%%" % round(conv * 100),
            "never completed" if stale is None else "%.0f min" % stale))

    if args.stale_min is not None:
        fleet = fleet_stale_minutes(seats)
        if fleet is None:
            print(f"\nFLEET UNPRODUCTIVE: no seat has EVER completed a task "
                  f"(threshold {args.stale_min:g} min). Alive is not working.")
            return 3
        if fleet > args.stale_min:
            print(f"\nFLEET UNPRODUCTIVE: {fleet:.0f} min since any seat completed anything "
                  f"(threshold {args.stale_min:g} min). Seats may be running; none is finishing.")
            return 3
        print(f"\nfleet productive: last completion {fleet:.0f} min ago "
              f"(threshold {args.stale_min:g} min)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
