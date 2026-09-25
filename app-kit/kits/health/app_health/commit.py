"""Which commit is THIS PROCESS running? Read from the checkout once, at import.

A deploy usually writes a stamp (a file, an environment variable) naming the commit it
shipped. The stamp is a CLAIM made by the deploy; the checkout at import time is what the
process actually LOADED. They diverge exactly when it matters: a deploy whose restart failed,
or whose rollback restored the previous commit, leaves the new stamp beside a process still
serving the old code -- and a liveness probe that reports the stamp says "live" about code
that is not running. So liveness reports the loaded commit, with the stamp beside it, and says
when the two disagree.

Read directly from ``.git`` (a worktree's ``gitdir:`` file, loose and packed refs) -- no
subprocess, once per process. '' when there is no checkout (an image without ``.git``); the
stamp is then the only evidence and liveness says so.
"""
from __future__ import annotations

import os
from pathlib import Path


def loaded_head_sha(base) -> str:
    """The commit ``base``'s checkout HEAD named when this was called; '' when unreadable."""
    try:
        base = Path(base)
        git = base / ".git"
        if git.is_file():                                   # a worktree: "gitdir: <path>"
            ref = git.read_text(encoding="utf-8").strip()
            if ref.startswith("gitdir:"):
                git = Path(ref.split(":", 1)[1].strip())
                if not git.is_absolute():
                    git = (base / git).resolve()
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head[:40]
        name = head.split(":", 1)[1].strip()
        common = git
        pointer = git / "commondir"
        if pointer.is_file():
            common = (git / pointer.read_text(encoding="utf-8").strip()).resolve()
        for root in (git, common):
            loose = root / name
            if loose.is_file():
                return loose.read_text(encoding="utf-8").strip()[:40]
        packed = common / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + name):
                    return line.split(" ", 1)[0][:40]
    except OSError:
        pass
    return ""


def prime(settings) -> str:
    """Read the loaded commit NOW -- call it at import of the module that serves liveness, so
    a checkout that moves later (a deploy whose restart then failed) cannot change the answer
    of a process that never reloaded. settings.LOADED_SHA wins when the project sets it."""
    global _LOADED
    if _LOADED is None:
        preset = getattr(settings, "LOADED_SHA", None)
        _LOADED = str(preset) if preset is not None else loaded_head_sha(
            getattr(settings, "BASE_DIR", os.getcwd()))
    return _LOADED


def identity(settings) -> dict:
    """{commit, commit_source, stamp_commit[, stamp_mismatch]} for a liveness payload.

    ``commit`` is the checkout HEAD this process loaded (``prime``), falling back to the deploy
    stamp (settings.BUILD_ID / DEPLOY_SHA / env BUILD_ID) when there is no checkout."""
    loaded = prime(settings)
    stamp = str(getattr(settings, "BUILD_ID", "") or getattr(settings, "DEPLOY_SHA", "")
                or os.environ.get("BUILD_ID", "") or "").strip()
    out = {"commit": loaded or stamp or "unknown",
           "commit_source": "checkout" if loaded else ("stamp" if stamp else "none"),
           "stamp_commit": stamp or None}
    if loaded and stamp and not (loaded.startswith(stamp) or stamp.startswith(loaded)):
        out["stamp_mismatch"] = True
    return out


_LOADED = None
