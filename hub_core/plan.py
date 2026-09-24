"""One definition of what a task's plan says about the WORK -- shared by every counter.

A plan is the worker's checklist, and its done/total ratio is the number every surface shows
for "how far along is this". Schedulers also write rows onto that plan about their OWN run: the
hub handing back a task whose lease lapsed, a launcher recording that a worker timed out or was
reaped. Counted raw, those rows make a task look MORE complete every time a worker dies on it --
a task abandoned three times reads as further along than one abandoned once, so the number moves
the wrong way under exactly the conditions a completeness count exists to catch.

So a row is structurally marked (`lifecycle: true`, or a `kind` from LIFECYCLE_KINDS), every
counter reads `work_steps`, and lifecycle rows are still SHOWN -- a task bouncing off the queue
three times is worth seeing -- just never ticked as work somebody did. A recurring lifecycle
event is ONE row counting itself (`times`), re-appended at the end so "the newest checkpoint is
a hand-back" keeps meaning "nobody has picked it up since".

Framework-free; the board's JavaScript mirrors LIFECYCLE_KINDS.
"""
from __future__ import annotations

import datetime as _dt

#: Kinds a SCHEDULER writes about its own run. Declared here so the next lifecycle event has
#: somewhere to be named instead of quietly becoming a work checkpoint.
LIFECYCLE_KINDS = frozenset({"handed_back", "lease_released", "reaped", "launcher_timeout",
                             "claim_expired", "lifecycle"})
#: Every kind a plan row may carry: a work checkpoint, a checkpoint that names the commit it
#: produced, and the lifecycle kinds above.
PLAN_KINDS = frozenset({"checkpoint", "pushed"}) | LIFECYCLE_KINDS


def is_lifecycle_step(step) -> bool:
    """Is this plan entry a scheduler talking about its own run, rather than work?"""
    if not isinstance(step, dict):
        return False
    return step.get("lifecycle") is True or str(step.get("kind") or "") in LIFECYCLE_KINDS


def work_steps(task_or_plan) -> list:
    """The plan with scheduler bookkeeping removed -- what a person means by the work."""
    plan = (task_or_plan.get("plan") if isinstance(task_or_plan, dict) else task_or_plan) or []
    return [s for s in plan if isinstance(s, dict) and not is_lifecycle_step(s)]


def plan_progress(task_or_plan) -> tuple:
    """``(done, total, lifecycle)``: work checkpoints done, work checkpoints declared, and how
    many plan rows are scheduler bookkeeping, so a surface can name that share."""
    plan = (task_or_plan.get("plan") if isinstance(task_or_plan, dict) else task_or_plan) or []
    work = work_steps(plan)
    return (sum(1 for s in work if s.get("done")), len(work),
            sum(1 for s in plan if isinstance(s, dict) and is_lifecycle_step(s)))


def recorded_shas(task: dict) -> set:
    """Commits the task itself recorded on its plan (`step --sha`), lowercased.

    Structured fields only: prose notes and borrowed evidence links can describe another task's
    work, and inferring authorship from them attributes one task's commit to another."""
    out = set()
    for step in (task or {}).get("plan") or []:
        if not isinstance(step, dict) or is_lifecycle_step(step):
            continue
        sha = str(step.get("sha") or "").strip().lower()
        if 7 <= len(sha) <= 40 and all(c in "0123456789abcdef" for c in sha):
            out.add(sha)
    return out


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def with_lifecycle_row(task: dict, kind: str, step: str, note: str, when: str = "") -> list:
    """The task's plan with ONE self-counting lifecycle row of `kind`, re-appended at the end.

    Repeats collapse into that row (`times` sums), so three hand-backs read as a pattern rather
    than as three more checkpoints."""
    if kind not in LIFECYCLE_KINDS:
        raise ValueError("not a lifecycle kind: %r" % kind)
    plan = [dict(s) for s in ((task or {}).get("plan") or []) if isinstance(s, dict)]
    prior = [s for s in plan if s.get("kind") == kind]
    times = sum(max(1, int(s.get("times") or 1)) for s in prior) + 1
    plan = [s for s in plan if s.get("kind") != kind]
    text = str(note or "")[:600]
    if times > 1:
        text = ("[%s %d times] " % (kind.replace("_", " "), times) + text)[:600]
    plan.append({"step": str(step or kind)[:120], "done": True, "note": text, "kind": kind,
                 "lifecycle": True, "times": times, "note_at": when or _now_iso()})
    return plan
