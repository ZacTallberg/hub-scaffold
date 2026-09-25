"""Lane locks: single-flight held by a HEARTBEAT, a short and a long lane, machine-sized slots.

Why a heartbeat and not an age. A lock that is "dead after N minutes from creation" is right only
while every run is shorter than N; the first hour-long run lets a second launcher take the lock
mid-run and two unattended sessions work one item in one repository at once. So the holder touches
its lock file every ``LOCK_BEAT_S`` while its session lives, and a lock is taken over only when
BOTH are true: nobody has touched it for ``LOCK_STALE_S`` AND its recorded pid is gone. Unknown
liveness reads as alive — a lock we cannot prove dead is a lock we do not steal.

Why two lanes. One lock made a one-minute answer wait behind a ninety-minute build. Questions
(and anything else short) run in the SHORT lane; tasks run in the LONG lane.

Why a one-slot ATTENTION lane. Several needs-attention conditions often share ONE cause (three
apps missing the same liveness route read as three items). On the long lane's parallel slots each
got its own responder, all three fixed the same thing side by side and one pushed an identical
file. In series the second run starts after the first fix is on the board and finds its item
already cleared, so attention runs one at a time in a lane of its own.

Why slots. A fixed number of long slots holds a 32-thread workstation to the same throughput as a
4-core laptop. The long lane gets half the logical cores or one slot per ``GB_PER_SLOT`` of memory,
whichever is smaller, never below the floor and never above the automatic ceiling; an explicit
``HUB_UNATTENDED_LONG_SLOTS`` wins either way (clamped 1..12).
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from ..process_lock import _pid_alive
from . import home

LOCK_STALE_S = 900
LOCK_BEAT_S = 60
LONG_SLOTS_FLOOR = 2
LONG_SLOTS_MAX = 6
GB_PER_SLOT = 5          # one agent session plus the tests it runs

SHORT, LONG, ATTENTION = "short", "long", "attention"


def lane_for(kind: str) -> str:
    """A task has the long lane; a needs-attention item its own one-slot lane; everything else
    is short."""
    if kind == "attention":
        return ATTENTION
    return LONG if kind == "task" else SHORT


def machine_memory_gb() -> float:
    """Physical memory in GB, stdlib only; 0.0 when it cannot be read."""
    try:
        if os.name == "nt":
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = _MemStatus()
            status.dwLength = ctypes.sizeof(_MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.ullTotalPhys / float(1 << 30)
            return 0.0
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / float(1 << 30)
    except Exception:  # noqa: BLE001 - a machine we cannot measure gets the floor
        return 0.0


def long_lane_slots() -> tuple[int, str]:
    """(slots, how they were decided) for the long lane on THIS machine."""
    try:
        explicit = int(os.environ.get("HUB_UNATTENDED_LONG_SLOTS", "0") or 0)
    except ValueError:
        explicit = 0
    if explicit > 0:
        return max(1, min(12, explicit)), "HUB_UNATTENDED_LONG_SLOTS=%d" % explicit
    by_cpu = (os.cpu_count() or 2) // 2
    memory = machine_memory_gb()
    by_mem = int(memory // GB_PER_SLOT) if memory > 0 else by_cpu
    slots = max(LONG_SLOTS_FLOOR, min(LONG_SLOTS_MAX, by_cpu, by_mem))
    return slots, "%d logical cores -> %d, %.0f GB -> %d; floor %d, ceiling %d" % (
        os.cpu_count() or 0, by_cpu, memory, by_mem, LONG_SLOTS_FLOOR, LONG_SLOTS_MAX)


def slot_paths(lane: str) -> list[Path]:
    base = home()
    if lane == LONG:
        count, _why = long_lane_slots()
        return [base / ("lane-long-%d.lock" % index) for index in range(1, count + 1)]
    if lane == ATTENTION:
        return [base / "lane-attention.lock"]
    return [base / "lane-short.lock"]


def _holder(path: Path) -> int:
    try:
        return int((path.read_text(encoding="ascii").strip() or "0").split()[0])
    except (OSError, ValueError):
        return 0


class LaneLock:
    """One held slot of one lane. ``acquire()`` takes the first free slot atomically (O_EXCL: the
    check and the claim are one operation, so two launchers seconds apart cannot both pass)."""

    def __init__(self, lane: str):
        self.lane = lane
        self.path: Path | None = None

    def acquire(self) -> bool:
        for slot in slot_paths(self.lane):
            try:
                if (slot.exists() and time.time() - slot.stat().st_mtime >= LOCK_STALE_S
                        and not _pid_alive(_holder(slot))):
                    slot.unlink()          # no heartbeat AND no process: a dead run's lock
            except OSError:
                pass
            try:
                fd = os.open(str(slot), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except OSError:
                continue
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(str(os.getpid()))
            self.path = slot
            return True
        return False

    def beat(self) -> None:
        if self.path is not None:
            try:
                os.utime(str(self.path), None)
            except OSError:
                pass

    def release(self) -> None:
        path, self.path = self.path, None
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.release()


def status() -> dict:
    """Every slot of both lanes: free, held (pid alive, beat age), or stale."""
    slots, why = long_lane_slots()
    rows = []
    for lane in (SHORT, LONG, ATTENTION):
        for path in slot_paths(lane):
            if not path.exists():
                rows.append({"lane": lane, "slot": path.name, "state": "free"})
                continue
            try:
                beat_age = int(time.time() - path.stat().st_mtime)
            except OSError:
                beat_age = None
            pid = _holder(path)
            alive = _pid_alive(pid)
            state = "held" if alive or (beat_age is not None and beat_age < LOCK_STALE_S) else "stale"
            rows.append({"lane": lane, "slot": path.name, "state": state, "pid": pid,
                         "pid_alive": alive, "beat_age_s": beat_age})
    return {"long_slots": slots, "long_slots_from": why, "stale_after_s": LOCK_STALE_S,
            "beat_every_s": LOCK_BEAT_S, "slots": rows}
