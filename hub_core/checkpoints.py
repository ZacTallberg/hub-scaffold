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

#: Kinds a SCHEDULER writes about its own run. Declared here so the next lifecycle event has
#: somewhere to be named instead of quietly becoming a work checkpoint.
LIFECYCLE_KINDS = frozenset({"handed_back", "lease_released", "reaped", "launcher_timeout",
                             "claim_expired", "lifecycle"})

#: Every kind the schema accepts; `checkpoint` is the default for a reported step.
KINDS = frozenset({"checkpoint", "pushed", "deployed"}) | LIFECYCLE_KINDS

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


def work_steps(task_or_plan) -> list:
    """The plan with placeholders and lifecycle rows removed — what a person means by the work."""
    plan = (task_or_plan.get("plan") if isinstance(task_or_plan, dict) else task_or_plan) or []
    return [s for s in plan if isinstance(s, dict)
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


def apply_step(plan, *, step=None, note="", kind="checkpoint", sha="", pipeline_id="",
               pipeline_url="", at="") -> tuple:
    """Record one checkpoint on a COPY of `plan`; return ``(new_plan, target_row)``.

    The one implementation behind the client's `step` verb and the MCP `step_task` tool, so
    both land identical rows. Targeting: an integer-like `step` (1-based) or a text fragment;
    default = the first undone WORK step. A number past the end grows `auto` placeholders; a
    plan with nothing left (or no plan) takes a report carrying a note as a NEW checkpoint.
    Raises ValueError with a sentence a worker can act on."""
    if kind not in KINDS:
        raise ValueError("kind must be one of %s" % sorted(KINDS))
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
    elif wanted:
        target = next((s for s in rows if wanted.lower() in str(s.get("step", "")).lower()), None)
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
    target["done"] = True
    target.pop("auto", None)
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
