"""File replacement and appends that survive a concurrent reader on Windows.

``os.replace`` needs DELETE access to the destination it overwrites (and to the source it
moves). CPython opens files with a share mode that lets other processes read and write the same
file but not delete it, so any reader holding the destination at that instant makes the replace
fail with ``PermissionError`` (WinError 5), and anything holding the source -- a scanner touching
the freshly written temp file -- makes it fail with WinError 32.

That is contention, not a fault: the other handle closes microseconds later. An indexer or an
antivirus scanner holds handles no lock of ours can reach, so the fix is to wait the sharing
window out. A bounded retry is correct on both sides of the race; it fails loudly, with the
original error, when the handle is genuinely stuck. On POSIX neither call has this failure mode,
so nothing here retries there. Standard library only.
"""
from __future__ import annotations

import errno
import os
import time

# ERROR_ACCESS_DENIED: someone holds the destination open.
# ERROR_SHARING_VIOLATION / ERROR_LOCK_VIOLATION: someone holds the source open.
TRANSIENT_WINERRORS = frozenset({5, 32, 33})

DEFAULT_TIMEOUT_S = 2.0


def _transient_replace_error(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) in TRANSIENT_WINERRORS


def _transient_open_error(exc: OSError) -> bool:
    """Is this failure to OPEN a file the same sharing window, seen from the other call?

    ``open()`` does not report ``winerror``: it goes through the C runtime, which translates the
    Win32 error into ``errno`` and leaves ``winerror`` as ``None``. Holding a file with no share
    mode and then calling ``open(path, "a")`` raises ``PermissionError [Errno 13]`` with
    ``winerror=None``, so the ``TRANSIENT_WINERRORS`` test that guards :func:`replace` can never
    match here -- a retry written in that idiom re-raises on the first attempt and fixes nothing.

    Key on ``EACCES`` on Windows instead. On POSIX an ``EACCES`` from ``open`` is a real
    permission decision that no amount of waiting changes, so nothing retries there.
    """
    return os.name == "nt" and exc.errno == errno.EACCES


def replace(src, dst, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
    """``os.replace`` that waits out a transient Windows sharing window.

    Re-raises the original error unchanged once *timeout_s* is spent, so a real permission
    problem (a read-only file, a directory ACL) still surfaces as itself.
    """
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
    """Append *text* to *path*, waiting out a transient Windows sharing window on the open.

    An append-only log cannot be written by rewrite-and-replace -- that is precisely what a
    lockless tailing reader must never see -- so the canonical write is ``open(path, "a")``,
    and that open loses to the same sharing window as :func:`replace`.

    ONLY THE OPEN IS RETRIED. Once the handle exists the write runs exactly once: retrying around
    a partially completed write would append a torn line to an append-only log, a far worse
    outcome than the error this replaces. The caller supplies the trailing newline; *newline*
    is passed to ``open`` unchanged, so an existing file keeps its line-ending convention.
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
