"""THE PROMOTION LANE: finished work held back from live, ageing in public until evidence frees it.

Some changes must not ship the moment they are written: a change to what is STORED or how it is
FOUND, a migration that must be rehearsed, anything one revert cannot undo. Holding such a change
back is right -- and without a record, "held" means a commit sitting in somebody's local
worktree: no age, no owner, invisible to the board, and gone the moment the worktree is cleaned.
"There is no process to push it live; it gets stuck undeployed."

A hold is a RECORD, and it makes three promises the worktree could not:

* **It is fetchable.** A hold refuses a commit nobody else can fetch, and the refusal names the
  fix (push it to a parking branch). The client that holds the repository attests it, and the
  Hub confirms through its own commit resolver when it can -- neither end is trusted alone.
  PUSHED IS THE DEFAULT, UNPUSHED IS A RECORDED EXCEPTION: a repository can have nowhere safe to
  park a commit, so a hold may say WHY it is on no remote -- and is then marked "ON ONE DISK
  ONLY" and ages four times faster. It may be allowed; it may never read as fine.
* **It ages in public.** Every open hold is on the board with who holds it and how long it has
  stood, and its urgency climbs with its age (``urgency``).
* **It is promoted with evidence.** ``promote`` demands the pipeline, sha or URL of the rebuild
  that actually ran; the schema refuses a promotion without it. The queue empties only for work
  that was promoted, never because somebody tired of the row.

Pure and standard-library only; the adapter supplies the state, the resolver and the append.
"""
from __future__ import annotations

import datetime as _dt
import re
import time

REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*(?:/[A-Za-z0-9][A-Za-z0-9._\-]*)*$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
GAP_RE = re.compile(r"^[a-z0-9][a-z0-9-]*:gap:[a-z0-9][a-z0-9._-]*$")

#: Age at which an open hold escalates, in days: info on day one, warn after two, critical after
#: a week. An unpushed hold ages FOUR times faster, because it is one deleted directory from gone.
WARN_AFTER_D = 2
CRITICAL_AFTER_D = 7
UNPUSHED_FACTOR = 4


def local_id(repo: str, sha: str) -> str:
    """One hold per (repo, sha): re-holding the same commit amends its record rather than minting
    a second row that ages separately from the first."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(repo).lower()).strip("-")
    return "%s--%s" % (slug[:90], str(sha).lower()[:12])


def _epoch(stamp) -> float:
    text = str(stamp or "").strip().replace("Z", "+00:00")
    if not text:
        return 0.0
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.timestamp()


def urgency(row: dict) -> str:
    """info / warn / critical from the hold's age -- on the clock an unpushed hold runs FASTER."""
    days = (row.get("age_s") or 0) / 86400.0
    if row.get("reachable") == "unpushed":
        days *= UNPUSHED_FACTOR
    if days >= CRITICAL_AFTER_D:
        return "critical"
    if days >= WARN_AFTER_D:
        return "warn"
    return "info"


def rows(state) -> list:
    out = [e for e in ((state or {}).get("entities") or {}).values()
           if isinstance(e, dict) and e.get("type") == "held"]
    return sorted(out, key=lambda e: str(e.get("id") or ""))


def queue(state, now: float | None = None) -> list:
    """The OPEN holds, OLDEST FIRST, each carrying its age, its holder and its urgency."""
    now = time.time() if now is None else now
    out = []
    for e in rows(state):
        if e.get("status") != "held":
            continue
        prov = e.get("provenance") or {}
        held_at = str(prov.get("created_at") or prov.get("updated_at") or "")
        born = _epoch(held_at)
        row = {"id": e.get("id"), "repo": e.get("repo"), "sha": e.get("sha"),
               "branch": e.get("branch") or "", "title": e.get("title") or "",
               "reason": e.get("reason") or "", "rebuild": e.get("rebuild") or "",
               "reachable": e.get("reachable") or "",
               "unpushed_reason": e.get("unpushed_reason") or "",
               "local_path": e.get("local_path") or "",
               "from_gap": e.get("from_gap") or "",
               "agent": str(prov.get("agent") or "").lower(),
               "held_at": held_at,
               "age_s": max(0, int(now - born)) if born else 0}
        row["urgency"] = urgency(row)
        out.append(row)
    out.sort(key=lambda r: (-r["age_s"], str(r["id"])))
    return out


def detail(row: dict) -> str:
    """What one held row says about itself, in one sentence a reader can act on."""
    reason = str(row.get("reason") or "held")
    rebuild = str(row.get("rebuild") or "")
    if row.get("reachable") == "unpushed":
        where = row.get("local_path") or ""
        text = ("ON ONE DISK ONLY (%s)%s. %s. Before it can go live: %s"
                % (row.get("unpushed_reason") or "reason not recorded",
                   " -- it is in " + where if where else "", reason, rebuild))
    else:
        text = "%s. Before it can go live: %s" % (reason, rebuild)
    if row.get("from_gap"):
        text += " (answers %s)" % row["from_gap"]
    return text[:400]


def validate_hold(b: dict) -> tuple:
    """``(clean_fields, error)`` for a hold request, before any resolver is asked."""
    repo = str(b.get("repo") or "").strip()
    sha = str(b.get("sha") or "").strip().lower()
    if not REPO_RE.match(repo):
        return None, ("bad_repo", "repo is the project path a reader can fetch, e.g. team/budget-app")
    if not SHA_RE.match(sha):
        return None, ("bad_sha", "sha is a commit hash (7-40 hex characters)")
    reason = str(b.get("reason") or "").strip()
    rebuild = str(b.get("rebuild") or "").strip()
    if not reason:
        return None, ("need_reason", "say why it may not go live yet")
    if not rebuild:
        return None, ("need_rebuild", "name what must be rebuilt, re-indexed or re-materialized "
                                      "and PROVEN before this can go live. Nothing to rebuild "
                                      "means it was never a held-class change: ship it.")
    from_gap = str(b.get("from_gap") or "").strip()
    if from_gap and not GAP_RE.match(from_gap):
        return None, ("bad_gap", "from_gap is a gap id, e.g. <project>:gap:0003 -- it is what "
                                 "keeps the queue and the Gaps tab telling one story")
    return {"repo": repo, "sha": sha, "reason": reason, "rebuild": rebuild,
            "from_gap": from_gap}, None


def reachability(attested: bool, confirmed, unpushed_reason: str) -> tuple:
    """``(reachable, error)`` from the client's attestation, the resolver's True/False/None and
    an optional recorded exception. Refuses a commit nobody else could fetch unless the caller
    SAYS why it is on no remote."""
    if (confirmed is False or (confirmed is None and not attested)) and not unpushed_reason:
        known = ("the Hub's commit resolver does not have it" if confirmed is False else
                 "the Hub could not ask whether it is pushed, and the caller did not attest it")
        return None, ("sha_not_pushed",
                      "%s. A commit that exists only in one worktree is what this record exists "
                      "to prevent: push it to a parking branch and hold it again. If the "
                      "repository has nowhere safe to park it, hold it with an unpushed_reason, "
                      "which records the exception instead of hiding it." % known)
    if unpushed_reason and not (attested or confirmed):
        return "unpushed", None
    return ("both" if (attested and confirmed) else ("resolver" if confirmed else "client")), None
