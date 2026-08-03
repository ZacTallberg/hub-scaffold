"""Small cross-process file lock used by Hub runtime sidecars.

The lock is deliberately independent of SQLite and third-party packages.  File creation with
``O_CREAT | O_EXCL`` is the compare-and-swap primitive on both Windows and POSIX.  A recorded PID
lets a waiter distinguish a dead owner from a merely slow one; elapsed time is only a far-off
ceiling for a genuinely wedged live process.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

_THREAD_LOCKS: dict[str, threading.RLock] = {}
_DEPTH: dict[str, int] = {}


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _pid_alive(pid: int | None) -> bool:
    """Conservatively report whether *pid* still exists without ever signalling it."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel = ctypes.windll.kernel32
            handle = kernel.OpenProcess(0x1000, False, int(pid))  # query-only access
            if not handle:
                return kernel.GetLastError() != 87  # ERROR_INVALID_PARAMETER: no such process
            code = ctypes.c_ulong()
            ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel.CloseHandle(handle)
            return (not ok) or code.value == 259  # STILL_ACTIVE
        except Exception:
            return True
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


class ProcessFileLock:
    """Re-entrant-in-process, exclusive-across-processes lock.

    ``directory`` is created if necessary.  Empty lock files are release markers left behind
    when Windows temporarily refuses an unlink; they are reclaimed immediately.  Legacy or
    unreadable lock files use a short mtime grace before reclamation.
    """

    LEGACY_STALE_S = 30.0
    DEFAULT_CEILING_S = 300.0

    def __init__(self, directory, name=".runtime.lock", timeout=None):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / name
        self.timeout = self.DEFAULT_CEILING_S if timeout is None else float(timeout)
        self._key = _key(self.path)
        self._thread_lock = _THREAD_LOCKS.setdefault(self._key, threading.RLock())

    def _holder_pid(self):
        try:
            raw = self.path.read_text(encoding="ascii").strip()
        except OSError:
            return None
        return int(raw) if raw.isdigit() else None

    def _breakable(self):
        pid = self._holder_pid()
        if pid is not None:
            return not _pid_alive(pid)
        try:
            stat = self.path.stat()
        except OSError:
            return False
        if stat.st_size == 0:
            return True
        return time.time() - stat.st_mtime > self.LEGACY_STALE_S

    def __enter__(self):
        self._thread_lock.acquire()
        if _DEPTH.get(self._key, 0) == 0:
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    try:
                        os.write(fd, str(os.getpid()).encode("ascii"))
                    finally:
                        os.close(fd)
                    break
                except FileExistsError:
                    if self._holder_pid() == os.getpid():
                        # A prior Windows unlink may have failed after our critical section ended.
                        try:
                            self.path.unlink()
                        except OSError:
                            pass
                        continue
                    if self._breakable():
                        try:
                            self.path.unlink()
                        except OSError:
                            pass
                        continue
                except PermissionError:
                    pass  # Windows delete-pending window: ordinary contention.
                if time.monotonic() >= deadline:
                    self._thread_lock.release()
                    raise TimeoutError(f"runtime lock busy: {self.path}")
                time.sleep(0.005)
        _DEPTH[self._key] = _DEPTH.get(self._key, 0) + 1
        return self

    def __exit__(self, *_exc):
        depth = _DEPTH.get(self._key, 1) - 1
        _DEPTH[self._key] = depth
        if depth == 0:
            try:
                self.path.unlink()
            except OSError:
                # Mark released if an indexer/scanner has the file open on Windows.
                try:
                    self.path.write_text("", encoding="ascii")
                except OSError:
                    pass
        self._thread_lock.release()
        return False
