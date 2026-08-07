"""Pull discipline for the ready queue (priority-aging-pull-discipline): the hub hands out the
highest EFFECTIVE-priority ready task, where effective priority is the base urgency boosted by
age past a threshold — old work rises instead of rotting — and, when the board is saturated,
cosmetic/enrichment kinds are shed from `next` (they stay on the board; they are just not what a
scarce worker should pull). Stack-neutral, pure functions over folded tasks + flags.

Tuning invariants:
- the age boost is CAPPED below one priority band, so an aged P2 overtakes fresh same-band work
  (and any cosmetic peer) but never a fresh P1/P0 — priority still rules across bands;
- shedding never empties the queue: an all-cosmetic board keeps handing out cosmetic work
  (shedding is triage, not starvation).
"""
import datetime

PRI = {"P0": 100, "P1": 60, "P2": 30, "P3": 10}
AGE_THRESHOLD_H = 24          # younger than this earns no boost
AGE_BOOST_PER_DAY = 10        # per full day past the threshold
AGE_BOOST_CAP = 25            # < one priority band (P2->P1 is +30), so bands still rule
COSMETIC_KINDS = frozenset({"content", "corpus"})
SATURATION_READY = 12         # more ready work than the fleet absorbs -> shed cosmetic


def _now(now=None):
    return now or datetime.datetime.now(datetime.timezone.utc)


def age_hours(task, now=None):
    created = ((task.get("provenance") or {}).get("created_at")) or ""
    try:
        t = datetime.datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (_now(now) - t).total_seconds() / 3600.0)


def age_boost(task, now=None):
    past = age_hours(task, now) - AGE_THRESHOLD_H
    if past <= 0:
        return 0
    return min(AGE_BOOST_CAP, int(past // 24 + 1) * AGE_BOOST_PER_DAY)


def effective_priority(task, flags=None, now=None):
    """Base urgency (priority weight + downstream-impact, as derive() computes it) plus the
    age boost. Falls back to the raw priority weight when flags carry no urgency."""
    f = (flags or {}).get(task.get("id"), {}) if flags else {}
    base = f.get("urgency") or PRI.get((task.get("priority") or "").upper(), 20)
    return base + age_boost(task, now)


def is_cosmetic(task):
    return (task.get("work_kind") or "") in COSMETIC_KINDS


def normalized_touches(task):
    """Return the task's declared file/surface affinity as a normalized set."""
    raw = task.get("touches") or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(item).strip().replace("\\", "/").lower()
            for item in raw if str(item).strip()}


def touches_busy(task, busy_touches=None):
    return bool(normalized_touches(task) & set(busy_touches or ()))


def pull_order(tasks, flags=None, now=None, busy_touches=None):
    """Prefer non-conflicting work, then highest effective priority. Touch affinity is a soft
    anti-collision signal: it never hides work and becomes inert when no live seat declares a
    touch."""
    def key(t):
        f = (flags or {}).get(t.get("id"), {}) if flags else {}
        return (touches_busy(t, busy_touches), -effective_priority(t, flags, now),
                f.get("pickup_rank", 10**9), t.get("id", ""))
    return sorted(tasks, key=key)


def order_ready(tasks, flags=None, now=None, busy_touches=None):
    """The full pull discipline for the READY queue: order by effective priority, and shed
    cosmetic kinds under saturation — unless that would empty the queue entirely."""
    ordered = pull_order(tasks, flags, now, busy_touches)
    if len(ordered) >= SATURATION_READY:
        kept = [t for t in ordered if not is_cosmetic(t)]
        if kept:
            return kept
    return ordered
