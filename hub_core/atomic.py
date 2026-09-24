"""Atomic file replacement, reads and appends that survive a concurrent reader on Windows.

Every sidecar the hub keeps beside its ledger (task leases, presence rows, the error queue's
acknowledgements, the credential registry, per-person and per-app preference stores) is
written the same way: write a temp file, then ``os.replace`` it over the destination. On POSIX
that rename cannot fail because somebody is reading. On Windows it can:

* ``os.replace`` needs DELETE access to the destination it overwrites. CPython opens files with
  a share mode that lets other processes read and write but NOT delete, so ANY reader holding
  the destination at that instant makes the replace fail with ``PermissionError`` (WinError 5).
* Anything holding the SOURCE -- an indexer or an antivirus scanner touching the fresh temp
  file -- makes it fail with WinError 32 (sharing violation) or 33 (lock violation).

That is contention, not a fault: the other handle closes microseconds later. Unhandled, it is a
500 on whichever write happened to collide -- a task claim, typically, because every board
render reads the lease files with no lock at all, and a lock the readers never take cannot
serialise them. Adding the lock to the readers would not close it either: a scanner holds
handles no lock of ours can reach.

So the window is treated as what it is and waited out. A bounded retry is correct on both sides
of the race and against the scanner; it re-raises the ORIGINAL error once the budget is spent,
so a genuinely stuck handle or a real permission problem still surfaces as itself. On POSIX
``winerror`` is ``None`` and nothing here retries.

Standard library only (``hub_core`` stays framework-free).
"""
from __future__ import annotations

import errno
import json
import os
import time
import uuid
from pathlib import Path

#: ERROR_ACCESS_DENIED (someone holds the destination), ERROR_SHARING_VIOLATION and
#: ERROR_LOCK_VIOLATION (someone holds the source).
TRANSIENT_WINERRORS = frozenset({5, 32, 33})

DEFAULT_TIMEOUT_S = 2.0


def _transient_replace_error(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) in TRANSIENT_WINERRORS


def _transient_open_error(exc: OSError) -> bool:
    """The same sharing window, seen from ``open()``.

    ``open()`` goes through the C runtime, which translates the Win32 error into ``errno`` and
    leaves ``winerror`` as ``None`` -- so the WinError test used by :func:`replace` can never
    match here. On Windows key on ``EACCES``; on POSIX an ``EACCES`` from ``open`` is a real
    permission decision that no amount of waiting changes, so nothing retries there.
    """
    return os.name == "nt" and exc.errno == errno.EACCES


def replace(src, dst, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
    """``os.replace`` that waits out a transient Windows sharing window, then re-raises the
    original error unchanged."""
    src, dst = os.fspath(src), os.fspath(dst)
    deadline = time.monotonic() + max(0.0, timeout_s)
    delay = 0.002
    while True:
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            if not _transient_replace_error(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.05)


def write_bytes(path, data: bytes, *, fsync: bool = True,
                timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
    """Write ``data`` to ``path`` atomically and durably, never leaving a temp file behind.

    The temp name carries the pid and a random suffix so two writers never fight over one temp,
    and it never ends in ``.json``: several readers glob their directory for ``*.json`` and would
    otherwise read a half-written file as a record. Without the fsync, a crash between the data
    write and the rename can leave a zero-length file where a valid one used to be.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        replace(tmp, path, timeout_s=timeout_s)
    finally:
        # An abandoned temp never becomes the destination; leaving it is how a state
        # directory silently fills with orphans.
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def write_text(path, text: str, *, encoding: str = "utf-8", fsync: bool = True,
               timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
    write_bytes(path, text.encode(encoding), fsync=fsync, timeout_s=timeout_s)


def write_json(path, obj, *, fsync: bool = True, timeout_s: float = DEFAULT_TIMEOUT_S,
               **dump_kwargs) -> None:
    write_text(path, json.dumps(obj, **dump_kwargs), fsync=fsync, timeout_s=timeout_s)


def open_read(path, mode: str = "rb", *, timeout_s: float = DEFAULT_TIMEOUT_S, **kwargs):
    """``open(path, mode)`` for READING that waits out the same transient sharing window.

    The ledger's append path reads the file's tail before it writes (to allocate from the
    canonical tail, never a stale index), so a reader that fails instantly would turn a
    few-microsecond scanner hold into a spurious full rebuild and then an error. Returns the
    open file object; a genuinely stuck handle re-raises the original error after *timeout_s*.
    """
    deadline = time.monotonic() + max(0.0, timeout_s)
    delay = 0.002
    while True:
        try:
            return open(path, mode, **kwargs)
        except OSError as exc:
            if not _transient_open_error(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.05)


def append_line(path, text: str, *, encoding: str = "utf-8", newline=None, fsync: bool = True,
                timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
    """Append ``text`` to ``path``, waiting out the same sharing window on the OPEN only.

    An append-only log cannot be rewritten-and-replaced (a tailing reader must never see that),
    so the write is a plain ``open(path, "a")``. Only the open is retried: once the handle
    exists the write runs exactly once, because retrying around a partial write would append a
    torn line -- far worse than the error it replaced. ``text`` is written as given (the caller
    supplies any trailing newline), and ``newline`` is passed to ``open`` unchanged, so a caller
    that pinned a newline translation keeps byte-identical rows.
    """
    deadline = time.monotonic() + max(0.0, timeout_s)
    delay = 0.002
    while True:
        try:
            handle = open(path, "a", encoding=encoding, newline=newline)
        except OSError as exc:
            if not _transient_open_error(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.05)
            continue
        with handle:
            handle.write(text)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        return
