"""One truthful task-flow classifier shared by every queue and claim surface.

The classifier is deliberately pure.  Adapters supply the folded dependency flags and the
currently-live lease (if any); every consumer then receives the same state/reason vocabulary.
"""

TERMINAL = frozenset({"done", "dropped", "shadow"})
CLAIMABLE = frozenset({"todo", "in_progress"})


def classify(task, flags=None, lease=None):
    """Return ``{state, available, stale_reclaim, reason}`` for one task.

    Executable work needs a concrete acceptance statement, never a standing test. An optional
    verification command is a rare, transient critical-boundary probe and is not a pull gate.
    """
    task = task or {}
    flags = flags or {}
    status = str(task.get("status") or "todo").lower()

    def result(state, reason, available=False, stale=False):
        return {"state": state, "available": bool(available),
                "stale_reclaim": bool(stale), "reason": reason}

    if status in TERMINAL:
        return result("terminal", f"task status is {status}")
    if task.get("poison_blocked"):
        return result("poison", task.get("poison_reason") or "verification circuit is open")
    if flags.get("snoozed_until"):
        return result("snoozed", "waiting until " + str(flags["snoozed_until"]))
    if flags.get("deps_unmet"):
        return result("blocked", "unmet dependencies: " + ", ".join(flags["deps_unmet"]))
    if status == "blocked":
        return result("blocked", "task is explicitly blocked")
    if lease:
        return result("leased", "held by " + str(lease.get("agent") or "another worker"))
    if status not in CLAIMABLE:
        return result("not_claimable", f"task status is {status}")
    if task.get("work_kind") in {"product", "verification"} and not str(
            task.get("acceptance") or "").strip():
        return result("needs_spec", "executable work requires concrete acceptance")
    stale = status == "in_progress"
    return result("stale_reclaim" if stale else "ready",
                  "expired or released lease; ready to reclaim" if stale else "ready to pull",
                  available=True, stale=stale)
