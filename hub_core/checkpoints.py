"""What a task's plan says, read as TYPED checkpoints rather than prose.

A plan item is a checkpoint a worker declared or reported. Three kinds of row share that list
and only one of them is work:

* a WORK checkpoint — declared up front or reported with `step --note`;
* a PLACEHOLDER (``auto: true``) — grown so `step --step 7` can land on a numbered step nobody
  declared. Counting it made "N of N done" lie both ways: step in order and done == total after
  every single step; step out of order and the holes stay undone forever;
* a LIFECYCLE row — a scheduler talking about its own run (a hand-back, a released lease, a
  reaped session). Counted as work, a task abandoned three times reads as MORE complete than
  one abandoned once: the number moves the wrong way under exactly the conditions a
  completeness check exists to catch.

THE ONE DEFINITION every counter reads (the board, the task rows, the health classifier, the
client), so no two surfaces can disagree about a task's progress. Structured fields decide
(`kind`, `lifecycle`, `auto`); a text fallback exists only for rows written before the fields
did, because history cannot be re-stamped. Stdlib only.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

#: Kinds a SCHEDULER writes about its own run. Declared here so the next lifecycle event has
#: somewhere to be named instead of quietly becoming a work checkpoint.
LIFECYCLE_KINDS = frozenset({"handed_back", "lease_released", "reaped", "launcher_timeout",
                             "claim_expired", "lifecycle"})

#: What the MACHINE check said about a deployed build: a `verified` row (result pass | fail, the
#: sha it looked at, the artifact it read) or a requester's `rejected` ("still broken"). Outcomes
#: are written through their own route (POST /hub/api/task/outcome), never by `step`, and are
#: never work: they say whether the work landed, not that more of it was done.
OUTCOME_KINDS = frozenset({"verified", "rejected"})

#: Rows the SYSTEM writes about a task -- the deploy record's `deployed`, the outcome rows, and a
#: published hand-off's `pushed` (marked ``receipt: "handoff"``). A plan write never removes one
#: (``keep_receipts``).
RECEIPT_KINDS = frozenset({"deployed"}) | OUTCOME_KINDS

#: Every kind the schema accepts; `checkpoint` is the default for a reported step.
KINDS = frozenset({"checkpoint", "pushed", "deployed"}) | LIFECYCLE_KINDS | OUTCOME_KINDS

_PLACEHOLDER = re.compile(r"^step \d+$")
_LIFECYCLE_TEXT = re.compile(
    r"(?i)(?:handed back to the queue|responder run ended|launcher (?:handed|stood down|"
    r"refused)|stood down\b)")


_IDLE_MARK = re.compile(r"\[handed back \d+ times?, (\d+) idle\]")


def total_runs(step) -> int:
    """Every run a self-counting lifecycle row records: its ``times`` (at least one). A row
    written before ``times`` existed is one run."""
    if not isinstance(step, dict):
        return 0
    raw = step.get("times")
    if raw is None:
        return 1
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def charged_runs(step) -> int:
    """The runs a hand-back row CHARGES against the task's run cap: every run it records minus
    the idle ones. Zero is a real answer here (a row that so far recorded only idle runs)."""
    return max(0, total_runs(step) - idle_runs(step))


def idle_runs(step) -> int:
    """Runs a hand-back row records as IDLE -- runs that ended with no new checkpoint or push.
    Carried in the row's NOTE (``[handed back N times, M idle]``), never as a field of its own:
    the plan item schema is closed, and an extra field turns every hand-back into a refusal."""
    if not isinstance(step, dict):
        return 0
    m = _IDLE_MARK.search(str(step.get("note") or ""))
    return int(m.group(1)) if m else 0


def is_placeholder(step) -> bool:
    """A grown, never-reported row: not done, no note, and marked `auto` (or, for rows written
    before the flag, titled exactly 'step N')."""
    if not isinstance(step, dict) or step.get("done") or str(step.get("note") or "").strip():
        return False
    if step.get("auto") is True:
        return True
    return bool(_PLACEHOLDER.match(str(step.get("step") or "").strip().lower()))


def is_lifecycle(step) -> bool:
    """Is this row the scheduler talking about its own run rather than the work?"""
    if not isinstance(step, dict):
        return False
    if step.get("lifecycle") is True or str(step.get("kind") or "") in LIFECYCLE_KINDS:
        return True
    return bool(_LIFECYCLE_TEXT.search("%s %s" % (step.get("step") or "", step.get("note") or "")))


def is_receipt(step) -> bool:
    """Is this row one the SYSTEM wrote about the task (deployed, an outcome, a hand-off push)?"""
    if not isinstance(step, dict):
        return False
    kind = step.get("kind")
    return kind in RECEIPT_KINDS or (kind == "pushed" and step.get("receipt") == "handoff")


def _receipt_key(row) -> tuple:
    return (row.get("kind"), str(row.get("sha") or "").lower(), str(row.get("note_at") or ""))


def keep_receipts(old_plan, new_plan) -> list:
    """A plan write never removes a system receipt.

    Writers send the WHOLE plan, so a numbered step that landed on a receipt's index rewrote it in
    place: on the instance this was lifted from, a task's `deployed` row became an agent's
    `pushed` note and the task stopped counting as verified live. A receipt the old plan had and
    the new one lacks goes back at its index; the row that took that slot moves to the END, minus
    the fields it only inherited from the receipt. Applied in the fold, so the ledger is never
    rewritten and old events re-derive their receipts too."""
    old_plan = [r for r in (old_plan or []) if isinstance(r, dict)]
    new_plan = list(new_plan or [])
    have = {_receipt_key(r) for r in new_plan if is_receipt(r)}
    lost = [(i, r) for i, r in enumerate(old_plan) if is_receipt(r) and _receipt_key(r) not in have]
    if not lost:
        return new_plan
    plan, moved = list(new_plan), []
    for i, r in lost:
        if i >= len(plan) or is_receipt(plan[i]) or not isinstance(plan[i], dict):
            plan.append(r)
            continue
        took = plan[i]
        plan[i] = r
        own = {k: v for k, v in took.items() if k != "done" and r.get(k) != v}
        if own:
            own.setdefault("step", str(own.get("note") or "step")[:80])
            moved.append({"done": took.get("done", False), **own})
    return plan + moved


def work_steps(task_or_plan) -> list:
    """The plan with placeholders, lifecycle rows and outcome receipts removed — what a person
    means by the work."""
    plan = (task_or_plan.get("plan") if isinstance(task_or_plan, dict) else task_or_plan) or []
    return [s for s in plan if isinstance(s, dict) and s.get("kind") not in OUTCOME_KINDS
            and not is_lifecycle(s) and not is_placeholder(s)]


def progress(task_or_plan) -> dict:
    """``{done, total, lifecycle, placeholders}`` over the WORK steps, with the excluded shares
    named so a surface can say what it left out instead of silently shrinking."""
    plan = (task_or_plan.get("plan") if isinstance(task_or_plan, dict) else task_or_plan) or []
    rows = [s for s in plan if isinstance(s, dict)]
    work = work_steps(rows)
    return {"done": sum(1 for s in work if s.get("done")), "total": len(work),
            "lifecycle": sum(1 for s in rows if is_lifecycle(s)),
            "placeholders": sum(1 for s in rows if is_placeholder(s))}


def latest(task_or_plan, kind: str):
    """The most recent checkpoint of one kind (a `pushed` sha, a `deployed` record), or None."""
    plan = (task_or_plan.get("plan") if isinstance(task_or_plan, dict) else task_or_plan) or []
    hits = [s for s in plan if isinstance(s, dict) and s.get("kind") == kind]
    if not hits:
        return None
    return max(hits, key=lambda s: str(s.get("note_at") or ""))


def recorded_shas(task_or_plan) -> list:
    """Every commit a checkpoint names STRUCTURALLY (`sha`), oldest first, de-duplicated. Prose
    is deliberately not mined: a sha glued into a note is a guess, and a guess must never close
    a task."""
    plan = (task_or_plan.get("plan") if isinstance(task_or_plan, dict) else task_or_plan) or []
    out = []
    for s in plan:
        sha = str((s or {}).get("sha") or "").strip().lower() if isinstance(s, dict) else ""
        if sha and sha not in out:
            out.append(sha)
    return out


#: A checkpoint `due_at` further out than this is refused on write and ignored on read: a wait
#: that long is a task to hand back, not one to hold while the board looks away.
MAX_DUE_AHEAD_S = 7 * 86400


def due_epoch(ts):
    """A checkpoint's `due_at` as an epoch, or None when absent or not an ISO time (no zone =
    UTC)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def normalize_due(text, now=None) -> str:
    """`step --due` as a canonical UTC time ('...Z'), REFUSED (ValueError) unless it parses, is
    ahead of now, and is at most MAX_DUE_AHEAD_S out. A passed time means the checkpoint is
    owed now, so it is never accepted as a reason to wait."""
    raw = str(text or "").strip()
    epoch = due_epoch(raw)
    if epoch is None:
        raise ValueError("due must be an ISO time in UTC, e.g. 2030-01-08T18:00Z (got %r)" % raw)
    ahead = epoch - (time.time() if now is None else now)
    if ahead <= 0:
        raise ValueError("due %s has already passed: the checkpoint is owed now -- do it, or "
                         "step what stops it" % raw)
    if ahead > MAX_DUE_AHEAD_S:
        raise ValueError("due %s is more than %d days out; the board does not honour a wait that "
                         "long -- hand the task back instead" % (raw, MAX_DUE_AHEAD_S // 86400))
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def apply_step(plan, *, step=None, note="", kind="checkpoint", sha="", pipeline_id="",
               pipeline_url="", at="", due="") -> tuple:
    """Record one checkpoint on a COPY of `plan`; return ``(new_plan, target_row)``.

    The one implementation behind the client's `step` verb and the MCP `step_task` tool, so
    both land identical rows. Targeting: an integer-like `step` (1-based) or a text fragment;
    default = the first undone WORK step. A number past the end grows `auto` placeholders; a
    plan with nothing left (or no plan) takes a report carrying a note as a NEW checkpoint.

    `due` says the checkpoint is NOT done and cannot be before that time (an acceptance window,
    a read after an overnight run): the note is recorded, the row stays OPEN with `due_at`, and
    `task_health` reads the task as waiting rather than stalled until then. Without it such a
    task was indistinguishable from abandoned work, and the stall alarm paged its owner (or
    launched a run) every hour of the window, each able only to re-read the same wait. A due
    step names no commit: a push is a done checkpoint, so the two are refused together.
    Raises ValueError with a sentence a worker can act on."""
    if kind not in KINDS:
        raise ValueError("kind must be one of %s" % sorted(KINDS))
    if kind in OUTCOME_KINDS:
        raise ValueError("an outcome (%s) is recorded with `verify` / `reject` (POST "
                         "/hub/api/task/outcome), never as a step" % kind)
    due = normalize_due(due) if str(due or "").strip() else ""
    if due and (sha or pipeline_id or pipeline_url or kind == "pushed"):
        raise ValueError("due marks a checkpoint NOT done yet; a pushed commit is a done "
                         "checkpoint -- record them as two steps")
    sha = str(sha or "").strip().lower()
    if sha and not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        raise ValueError("sha must be a 7..40 character hexadecimal commit id")
    if pipeline_id and not str(pipeline_id).isdigit():
        raise ValueError("pipeline_id is the pipeline's numeric id")
    rows = [dict(s) for s in (plan or []) if isinstance(s, dict)]
    wanted = str(step or "").strip()
    target = None
    if wanted.isdigit():
        index = int(wanted)
        if index < 1:
            raise ValueError("step numbers start at 1")
        while len(rows) < index:
            rows.append({"step": "step %d" % (len(rows) + 1), "done": False, "auto": True})
        target = rows[index - 1]
        if is_receipt(target):
            # A receipt is the system's record, never a slot a step may overwrite: the report
            # lands as a NEW row instead (the fold would restore the receipt anyway).
            target = {"step": (str(note).strip().splitlines() or ["checkpoint"])[0][:120]
                      or "checkpoint"}
            rows.append(target)
    elif wanted:
        target = next((s for s in rows if not is_receipt(s)
                       and wanted.lower() in str(s.get("step", "")).lower()), None)
        if target is None:
            raise ValueError("no plan step matches %r" % wanted)
    else:
        target = next((s for s in work_steps(rows) if not s.get("done")), None)
    if target is None:
        if not str(note or "").strip():
            raise ValueError("every plan step is already done (or there is no plan) -- pass a "
                             "note to record a new checkpoint, or finish the task")
        target = {"step": str(note).strip().splitlines()[0][:120] or "checkpoint"}
        rows.append(target)
    target["done"] = not due
    target.pop("auto", None)
    if note and str(note) != str(target.get("note") or "") and not (sha or pipeline_id
                                                                   or pipeline_url):
        # A NEW note on a checkpoint that recorded a push is a new report, not that push. Kept,
        # the old sha made the board read the new checkpoint as pushing an old commit (and a
        # deploy close or a pipeline hold then keyed on a commit the new report never named).
        for key in ("sha", "pipeline_id", "pipeline_url"):
            target.pop(key, None)
        if target.get("kind") in ("pushed", "deployed"):
            target["kind"] = kind
    if due:
        target["due_at"] = due
    else:
        target.pop("due_at", None)
    if note:
        # Whole: the next reader acts on it; a surface that must fit a line shows a preview.
        target["note"] = str(note)
    if at:
        target["note_at"] = at
    if kind != "checkpoint" or target.get("kind"):
        target["kind"] = kind
    if sha:
        target["sha"] = sha
    if pipeline_id:
        target["pipeline_id"] = str(pipeline_id)
    if pipeline_url:
        target["pipeline_url"] = str(pipeline_url)[:300]
    return rows, target
