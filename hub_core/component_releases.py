"""Pinned component versions: an app can wear a hosted component AS IT WAS, while the master
moves on.

The hosted components are LINKED, never copied (``hub_core.components``), so a change to the
master reaches every adopting app on its next page load. That is the point of hosting -- and it
is also why a bad release of a shared header lands on every app at once, with nothing an app can
roll back to. A pin answers that without a second copy of anything: every version the hub ever
served is already in its repository's history, so a pinned file is read straight out of git::

    /hub/components/@<commit>/<name>/<file>      e.g. /hub/components/@1a2b3c4d5e6f/banner/banner.js

* ONLY SHIPPED HISTORY. The commit must be an ancestor of what is deployed (HEAD of the checkout
  the hub serves from). A branch nobody merged is never servable, nor anything a caller invents;
  an unknown, unshipped or unreadable commit is a 404 -- never a quiet fallback to the latest,
  which would un-pin the app without telling it.
* A WHOLE RELEASE. A component that loads its own parts from its own base finds them under the
  same ``@<commit>/`` prefix, so the parts are pinned with it, from the same commit.
* IMMUTABLE. The bytes at a commit cannot change: the answer is cached for a year, and kept in a
  bounded in-process cache after the first read (one ``git cat-file`` per file per process).
* VERSIONS ARE MEASURED, never declared: ``versions(name)`` lists the shipped commits that
  changed the component (newest first) with the main script's own ``VERSION`` constant at each,
  read from the diffs that changed that line (never by loading every old copy). A version is
  served by the NEWEST commit that still carried it -- what adopters ran while it was current.

It needs a git checkout with history where the hub runs (a shallow or exported image answers
every pin with 404 and ``versions`` with an empty list -- said, never guessed). Framework-free,
stdlib only; every git call is bounded and never raises.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from collections import OrderedDict
from pathlib import Path

from .components import ALLOWED_SUFFIXES, COMPONENTS

_RELEASE = re.compile(r"""\bVERSION\s*=\s*["']([0-9]+(?:\.[0-9]+){1,3})["']""")
_SHA = re.compile(r"[0-9a-f]{7,40}")
_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,80}")
_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
GIT_TIMEOUT = 8
VERSIONS_LIMIT = 60
VERSIONS_RECHECK_S = 60
BLOB_BUDGET = 48 * 1024 * 1024

_LOCK = threading.Lock()
_STATE: dict = {"repo": None, "rel": None, "head": None, "at": 0.0, "by_name": {}}
_RESOLVED: dict = {}                      # rev -> full sha, or "" when refused
_BLOBS: "OrderedDict[tuple, bytes]" = OrderedDict()
_BLOB_BYTES = [0]


def _git(repo, *args, input_bytes: bytes | None = None):
    try:
        return subprocess.run(["git", "-C", str(repo), *args], input=input_bytes,
                              capture_output=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None


def _locate():
    """(repository root, components dir relative to it as a posix path), or (None, None) when
    the components directory is not inside a git checkout."""
    with _LOCK:
        if _STATE["repo"] is not None:
            return _STATE["repo"], _STATE["rel"]
    r = _git(COMPONENTS, "rev-parse", "--show-toplevel")
    if r is None or r.returncode != 0:
        return None, None
    root = Path(r.stdout.decode("utf-8", "replace").strip())
    try:
        rel = COMPONENTS.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None, None
    with _LOCK:
        _STATE["repo"], _STATE["rel"] = root, rel
    return root, rel


def _head(repo) -> str | None:
    r = _git(repo, "rev-parse", "HEAD")
    sha = r.stdout.decode("ascii", "replace").strip() if r is not None else ""
    return sha if r is not None and r.returncode == 0 and _SHA.fullmatch(sha) else None


def resolve(rev: str) -> str:
    """The full sha of a SHIPPED commit (an ancestor of HEAD), or "". Never raises. A git
    failure is not cached: a hiccup is not a verdict."""
    rev = (rev or "").strip().lower()
    if not _SHA.fullmatch(rev):
        return ""
    with _LOCK:
        if rev in _RESOLVED:
            return _RESOLVED[rev]
    repo, _rel = _locate()
    if repo is None:
        return ""
    r = _git(repo, "rev-parse", "--verify", "--quiet", rev + "^{commit}")
    if r is None:
        return ""
    cand = r.stdout.decode("ascii", "replace").strip()
    full = ""
    if r.returncode == 0 and _SHA.fullmatch(cand):
        anc = _git(repo, "merge-base", "--is-ancestor", cand, "HEAD")
        if anc is None:
            return ""
        if anc.returncode == 0:
            full = cand
    with _LOCK:
        _RESOLVED[rev] = full
    return full


def _main_script(rel: str, name: str) -> str:
    return "%s/%s/%s.js" % (rel, name, name)


def _measure(repo, rel: str, name: str) -> list[dict]:
    """Shipped commits that changed ``name``, newest first, each with the VERSION it carried."""
    r = _git(repo, "log", "--first-parent", "-n", str(VERSIONS_LIMIT * 4), "--format=%H %cI",
             "HEAD", "--", "%s/%s" % (rel, name))
    if r is None or r.returncode != 0:
        return []
    commits = [ln.split(" ", 1) for ln in r.stdout.decode("utf-8", "replace").splitlines()
               if " " in ln]
    if not commits:
        return []
    bumps, current = {}, None
    r = _git(repo, "log", "--first-parent", "--format=@@C %H", "-G",
             r"VERSION\s*=\s*[\"'][0-9]", "-p", "-U0", "HEAD", "--", _main_script(rel, name))
    for line in (r.stdout.decode("utf-8", "replace").splitlines() if r is not None else []):
        if line.startswith("@@C "):
            current = line[4:].strip()
        elif current and line.startswith("+") and current not in bumps:
            m = _RELEASE.search(line)
            if m:
                bumps[current] = m.group(1)
    carried, cur = {}, None
    for sha, _date in reversed(commits):               # oldest first: carry the nearest bump
        cur = bumps.get(sha, cur)
        carried[sha] = cur
    out, seen = [], set()
    for sha, date in commits:                          # newest first
        version = carried.get(sha)
        if version:
            if version in seen:
                continue                               # an older commit of a version already listed
            seen.add(version)
        out.append({"version": version, "commit": sha, "short": sha[:12], "date": date})
        if len(out) >= VERSIONS_LIMIT:
            break
    return out


def versions(name: str) -> list[dict]:
    """Every shipped release of one component, newest first (``[]`` without a git history).
    Remembered until HEAD moves (re-checked at most once a minute)."""
    if not _NAME.fullmatch(name or ""):
        return []
    repo, rel = _locate()
    if repo is None:
        return []
    now = time.time()
    with _LOCK:
        fresh = now - _STATE["at"] < VERSIONS_RECHECK_S
    if not fresh:
        head = _head(repo)
        with _LOCK:
            if head != _STATE["head"]:
                _STATE["by_name"] = {}
                _RESOLVED.clear()                      # a moved HEAD can change "shipped"
            _STATE.update(head=head, at=now)
    with _LOCK:
        cached = _STATE["by_name"].get(name)
        head = _STATE["head"]
    if cached is not None:
        return cached
    measured = _measure(repo, rel, name) if head else []
    with _LOCK:
        _STATE["by_name"][name] = measured
    return measured


def file_at(rev: str, name: str, filename: str):
    """``(bytes, full sha)`` of one component file as it was at a shipped commit, or None."""
    if (not _NAME.fullmatch(name or "") or not _FILE.fullmatch(filename or "")
            or Path(filename).suffix.lower() not in ALLOWED_SUFFIXES):
        return None
    full = resolve(rev)
    if not full:
        return None
    key = (full, name, filename)
    with _LOCK:
        if key in _BLOBS:
            _BLOBS.move_to_end(key)
            return _BLOBS[key], full
    repo, rel = _locate()
    if repo is None:
        return None
    r = _git(repo, "cat-file", "blob", "%s:%s/%s/%s" % (full, rel, name, filename))
    if r is None or r.returncode != 0:
        return None
    body = r.stdout
    with _LOCK:
        _BLOBS[key] = body
        _BLOB_BYTES[0] += len(body)
        while _BLOB_BYTES[0] > BLOB_BUDGET and len(_BLOBS) > 1:
            _k, old = _BLOBS.popitem(last=False)
            _BLOB_BYTES[0] -= len(old)
    return body, full


def version_at(full: str, name: str) -> str:
    """The VERSION a component carried at a commit ("" when it declares none)."""
    for row in versions(name):
        if row["commit"] == full and row.get("version"):
            return row["version"]
    repo, rel = _locate()
    if repo is None:
        return ""
    r = _git(repo, "cat-file", "blob", "%s:%s" % (full, _main_script(rel, name)))
    if r is None or r.returncode != 0:
        return ""
    m = _RELEASE.search(r.stdout.decode("utf-8", "replace"))
    return m.group(1) if m else ""
