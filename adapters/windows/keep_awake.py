"""Hold a Windows wake lock so the machine does not sleep while a long unattended job runs.

A machine that sleeps under a job does not pause it: every wall-clock deadline advances across the
gap and fires on wake, so the job is killed having run a fraction of its bound, sockets are severed,
and every timeout it hit looks like a real fault. The only tell afterwards is an elapsed time that
is wildly wrong. Hold the lock BEFORE the long run, not after you have lost one.

Uses ``SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)``: the SYSTEM stays awake, the
display may still turn off. The lock belongs to this process and is released when it exits — no
persistent power setting to revert. It does not override logging off, shutdown, or a lid-close
action; those are separate policies. It does not keep an interactive session unlocked, and it is not
meant to: locking the screen leaves a running job running.

``python -m hub_core.unattended`` already holds the same lock for the length of each run; this
script covers a whole unattended window (a scheduler firing runs overnight).

    python keep_awake.py                 # hold until Ctrl-C / killed
    python keep_awake.py --minutes 720   # hold for N minutes, then release
"""
import argparse
import sys
import time
from datetime import datetime

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
# ES_DISPLAY_REQUIRED (0x00000002) is deliberately NOT set: the screen may sleep.


def log(message):
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--minutes", type=float, default=None,
                        help="release after N minutes (default: hold until killed)")
    args = parser.parse_args()
    if sys.platform != "win32":
        log("ERROR: this helper uses the Windows power API; on Linux use `systemd-inhibit`, "
            "on macOS `caffeinate`.")
        return 2

    import ctypes
    set_state = ctypes.windll.kernel32.SetThreadExecutionState
    if set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) == 0:
        log("ERROR: SetThreadExecutionState failed; the machine may still sleep.")
        return 1
    log("Wake lock HELD - the system will not sleep (the display may still turn off).")
    deadline = (time.time() + args.minutes * 60) if args.minutes else None
    if deadline:
        log(f"Releasing automatically in {args.minutes:g} minutes.")
    try:
        while True:
            set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)     # re-assert each cycle
            if deadline and time.time() >= deadline:
                break
            time.sleep(30 if not deadline else max(1, min(30, deadline - time.time())))
    except KeyboardInterrupt:
        pass
    finally:
        set_state(ES_CONTINUOUS)
        log("Wake lock RELEASED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
