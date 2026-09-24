"""How far a checkout is behind its remote — the one fact every tree-reading tool must say.

A tool that reads a WORKING TREE reports on what is on disk, which is not what CI, the host,
or another person sees. A verdict from a stale checkout reads exactly like a defect: it names a
real app, states a real problem, and sends someone to fix what was already fixed upstream.

Two uses, one function:

* a MEASURING tool annotates each verdict with ``stale_note(path)`` — empty when current or
  unknown, "[checkout is N behind origin/main — verdict may be stale]" when it lags;
* a PATCHING tool calls ``refuse_if_behind(path, anyway=...)`` before it writes, because
  rewriting a stale checkout produces a file that is neither the old shape nor the new one and
  that nobody ships.

Unknown (no git, no remote, a failed call) is reported as ``None`` and prints NOTHING — the
tool never claims a tree is current when it could not find out.

Nothing here fetches. Fetch first (``git fetch``), then measure: this module compares against
whatever ``origin/<branch>`` your last fetch recorded, and says so in its messages.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

DEFAULT_UPSTREAM = "origin/main"


def behind(path: Path | str, upstream: str = DEFAULT_UPSTREAM) -> int | None:
    """Commits ``upstream`` has that this checkout's HEAD lacks, or None when unknowable."""
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-list", "--count", f"HEAD..{upstream}"],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return int(out.stdout.strip())
    except ValueError:
        return None


def stale_note(path: Path | str, upstream: str = DEFAULT_UPSTREAM) -> str:
    """The suffix a measuring tool appends to a verdict row; empty when current or unknown."""
    lag = behind(path, upstream)
    if not lag:
        return ""
    return f"  [checkout is {lag} behind {upstream} (as of your last fetch) — verdict may be stale]"


class StaleCheckout(RuntimeError):
    """Raised by refuse_if_behind: the checkout lags its upstream and the caller would write."""


def refuse_if_behind(path: Path | str, *, anyway: bool = False,
                     upstream: str = DEFAULT_UPSTREAM) -> int | None:
    """Refuse to operate on a checkout that is behind ``upstream``; return the lag otherwise.

    ``anyway`` is for a checkout that is deliberately ahead, detached, or pinned for a reason
    the operator can state. Unknown staleness is not a refusal: a tree outside git is not
    behind anything, and refusing it would make the tool unusable on an export.
    """
    lag = behind(path, upstream)
    if lag and not anyway:
        raise StaleCheckout(
            f"{path} is {lag} commit(s) behind {upstream}. Refusing to write into a stale "
            f"checkout. Pull it, use a worktree at {upstream}, or pass --anyway if this "
            f"checkout is deliberately behind.")
    return lag
