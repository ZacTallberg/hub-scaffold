"""Compatibility-first pull discipline for the canonical ready frontier.

The Hub first derives readiness, dependency order, WIP, leases, priority, critical-path rank, and
age from canonical board state. Optional worker placement then removes incompatible work and uses
observed outcome fit only inside an equal urgency/critical-path cohort. A soft score can never make
blocked work ready or make an incompatible worker eligible.

Tuning invariants:
- the age boost is capped below one priority band, so old same-band work rises without erasing
  explicit priority;
- shedding never empties an all-cosmetic queue;
- missing worker facts cannot satisfy an explicit task requirement;
- tasks without routing requirements preserve the zero-profile pull path.
"""
import datetime
from collections import Counter

PRI = {"P0": 100, "P1": 60, "P2": 30, "P3": 10}
AGE_THRESHOLD_H = 24
AGE_BOOST_PER_DAY = 10
AGE_BOOST_CAP = 25
COSMETIC_KINDS = frozenset({"content", "corpus"})
SATURATION_READY = 12
RISK_LEVEL = {"low": 0, "moderate": 1, "high": 2, "critical": 3}
_RESOURCE_KEYS = ("tokens", "seconds", "cost_usd")
_OUTCOME_KEYS = ("quality", "latency_s", "cost_usd")

# Published verbatim by the MCP ``take_task`` tool. This is a per-pull placement declaration, not
# durable identity; scoped credentials or runs can supply the same facts later without overloading
# the reusable capability graph with mutable worker presence.
WORKER_PROFILE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "capabilities": {
            "type": "array", "maxItems": 64, "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "risk_clearance": {"enum": list(RISK_LEVEL)},
        "availability": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "available": {"type": "boolean"},
                "tokens": {"type": "integer", "minimum": 0},
                "seconds": {"type": "number", "minimum": 0},
                "cost_usd": {"type": "number", "minimum": 0},
            },
        },
        "localities": {
            "type": "array", "maxItems": 32, "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "outcomes": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "quality": {"type": "number", "minimum": 0, "maximum": 1},
                "latency_s": {"type": "number", "minimum": 0},
                "cost_usd": {"type": "number", "minimum": 0},
                "samples": {"type": "integer", "minimum": 1},
            },
        },
    },
}


def _now(now=None):
    return now or datetime.datetime.now(datetime.timezone.utc)


def age_hours(task, now=None):
    created = ((task.get("provenance") or {}).get("created_at")) or ""
    try:
        created_at = datetime.datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (_now(now) - created_at).total_seconds() / 3600.0)


def age_boost(task, now=None):
    past = age_hours(task, now) - AGE_THRESHOLD_H
    if past <= 0:
        return 0
    return min(AGE_BOOST_CAP, int(past // 24 + 1) * AGE_BOOST_PER_DAY)


def effective_priority(task, flags=None, now=None):
    """Return derived urgency plus a bounded age boost."""
    task_flags = (flags or {}).get(task.get("id"), {}) if flags else {}
    base = task_flags.get("urgency") or PRI.get((task.get("priority") or "").upper(), 20)
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


def _token_set(value, field, limit):
    if value is None:
        return frozenset()
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"worker.{field} must be an array of at most {limit} strings")
    out = []
    for item in value:
        token = str(item).strip().lower() if isinstance(item, str) else ""
        if not token or len(token) > 128:
            raise ValueError(
                f"worker.{field} entries must be non-empty strings up to 128 characters")
        out.append(token)
    if len(set(out)) != len(out):
        raise ValueError(f"worker.{field} entries must be unique")
    return frozenset(out)


def _number_map(value, field, allowed):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(f"worker.{field} contains unsupported fields")
    out = {}
    for key, raw in value.items():
        if key == "available":
            if not isinstance(raw, bool):
                raise ValueError("worker.availability.available must be boolean")
            out[key] = raw
            continue
        if key == "samples":
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
                raise ValueError("worker.outcomes.samples must be a positive integer")
            out[key] = raw
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
            raise ValueError(f"worker.{field}.{key} must be a non-negative number")
        if key == "quality" and raw > 1:
            raise ValueError("worker.outcomes.quality must be between 0 and 1")
        out[key] = float(raw)
    return out


def normalize_worker_profile(value=None):
    """Validate and canonicalize the bounded per-pull worker placement declaration."""
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ValueError("worker must be an object")
    allowed = {"capabilities", "risk_clearance", "availability", "localities", "outcomes"}
    if set(value) - allowed:
        raise ValueError("worker contains unsupported fields")
    risk = value.get("risk_clearance")
    if risk is not None and risk not in RISK_LEVEL:
        raise ValueError("worker.risk_clearance must be low, moderate, high, or critical")
    return {
        "capabilities": _token_set(value.get("capabilities"), "capabilities", 64),
        "risk_clearance": risk,
        "availability": _number_map(
            value.get("availability"), "availability", ("available",) + _RESOURCE_KEYS),
        "localities": _token_set(value.get("localities"), "localities", 32),
        "outcomes": _number_map(
            value.get("outcomes"), "outcomes", _OUTCOME_KEYS + ("samples",)),
    }


def _requirement_tokens(value):
    if not isinstance(value, list):
        return None
    tokens = []
    for item in value:
        token = str(item).strip().lower() if isinstance(item, str) else ""
        if not token:
            return None
        tokens.append(token)
    return frozenset(tokens)


def _outcome_score(requirements, profile):
    """Bound observed quality/latency/cost into 0..1; absent observations stay unmeasured."""
    outcomes = profile.get("outcomes") or {}
    weights = requirements.get("outcome_weights") or {}
    components = {
        "quality": outcomes.get("quality"),
        "latency": (1.0 / (1.0 + outcomes["latency_s"]))
                   if "latency_s" in outcomes else None,
        "cost": (1.0 / (1.0 + outcomes["cost_usd"]))
                if "cost_usd" in outcomes else None,
    }
    defaults = {"quality": 0.5, "latency": 0.25, "cost": 0.25}
    measured = [(name, value, float(weights.get(name, defaults[name])))
                for name, value in components.items() if value is not None]
    denominator = sum(weight for _, _, weight in measured)
    return (sum(value * weight for _, value, weight in measured) / denominator
            if denominator else 0.0)


def routing_fit(task, worker_profile=None):
    """Return hard compatibility, reasons, and soft fit for one already-ready task."""
    profile = worker_profile or {}
    requirements = task.get("routing") or {}
    availability = profile.get("availability") or {}
    reasons = []
    if availability.get("available") is False:
        reasons.append("worker_unavailable")
    if not requirements:
        return {"compatible": not reasons, "reasons": reasons,
                "outcome_score": 0.0, "locality_score": 0.0}
    if not isinstance(requirements, dict):
        return {"compatible": False, "reasons": ["routing_invalid"],
                "outcome_score": 0.0, "locality_score": 0.0}

    required_caps = _requirement_tokens(requirements.get("required_capabilities", []))
    if required_caps is None:
        reasons.append("routing_invalid")
    else:
        for missing in sorted(required_caps - (profile.get("capabilities") or frozenset())):
            reasons.append("capability_missing:" + missing)

    task_risk = requirements.get("risk")
    if task_risk:
        clearance = profile.get("risk_clearance")
        if clearance not in RISK_LEVEL:
            reasons.append("risk_clearance_unknown")
        elif task_risk not in RISK_LEVEL:
            reasons.append("routing_invalid")
        elif RISK_LEVEL[clearance] < RISK_LEVEL[task_risk]:
            reasons.append("risk_clearance_insufficient")

    budget = requirements.get("budget") or {}
    if not isinstance(budget, dict):
        reasons.append("routing_invalid")
    else:
        for key in _RESOURCE_KEYS:
            if key not in budget:
                continue
            if key not in availability:
                reasons.append("availability_unknown:" + key)
            elif availability[key] < budget[key]:
                reasons.append("availability_insufficient:" + key)

    locality = requirements.get("locality") or {}
    if not isinstance(locality, dict):
        reasons.append("routing_invalid")
        required_locality, preferred_locality = frozenset(), frozenset()
    else:
        required_locality = _requirement_tokens(locality.get("required", []))
        preferred_locality = _requirement_tokens(locality.get("preferred", []))
        if required_locality is None or preferred_locality is None:
            reasons.append("routing_invalid")
            required_locality, preferred_locality = frozenset(), frozenset()
    worker_locality = profile.get("localities") or frozenset()
    for missing in sorted(required_locality - worker_locality):
        reasons.append("locality_missing:" + missing)

    outcomes = profile.get("outcomes") or {}
    constraints = requirements.get("outcome_constraints") or {}
    if not isinstance(constraints, dict):
        reasons.append("routing_invalid")
    else:
        outcome_checks = (
            ("quality_min", "quality", lambda observed, limit: observed >= limit),
            ("latency_max_s", "latency_s", lambda observed, limit: observed <= limit),
            ("cost_max_usd", "cost_usd", lambda observed, limit: observed <= limit),
        )
        for constraint, observed_key, passes in outcome_checks:
            if constraint not in constraints:
                continue
            if observed_key not in outcomes:
                reasons.append("outcome_unknown:" + observed_key)
            elif not passes(outcomes[observed_key], constraints[constraint]):
                reasons.append("outcome_constraint:" + observed_key)

    locality_score = (len(preferred_locality & worker_locality) / len(preferred_locality)
                      if preferred_locality else 0.0)
    return {"compatible": not reasons, "reasons": sorted(set(reasons)),
            "outcome_score": _outcome_score(requirements, profile),
            "locality_score": locality_score}


def _frontier_key(task, flags=None, now=None, busy_touches=None):
    task_flags = (flags or {}).get(task.get("id"), {}) if flags else {}
    return (touches_busy(task, busy_touches), -effective_priority(task, flags, now),
            -int(task_flags.get("rank_u") or 0))


def pull_order(tasks, flags=None, now=None, busy_touches=None, routing_fits=None):
    """Order compatible ready work; placement fit breaks only a frontier-order tie."""
    def key(task):
        task_flags = (flags or {}).get(task.get("id"), {}) if flags else {}
        fit = (routing_fits or {}).get(task.get("id"), {})
        return (*_frontier_key(task, flags, now, busy_touches),
                -float(fit.get("locality_score") or 0),
                -float(fit.get("outcome_score") or 0),
                task_flags.get("pickup_rank", 10**9), task.get("id", ""))
    return sorted(tasks, key=key)


def route_ready(tasks, flags=None, now=None, busy_touches=None, worker_profile=None):
    """Filter one canonical ready frontier for a worker, then rank its compatible members."""
    profile = normalize_worker_profile(worker_profile)
    fits = {task.get("id"): routing_fit(task, profile) for task in tasks}
    compatible = [task for task in tasks if fits[task.get("id")]["compatible"]]
    ordered = pull_order(compatible, flags, now, busy_touches, routing_fits=fits)
    if len(ordered) >= SATURATION_READY:
        kept = [task for task in ordered if not is_cosmetic(task)]
        if kept:
            ordered = kept
    reasons = Counter(reason for fit in fits.values() if not fit["compatible"]
                      for reason in fit["reasons"])
    selected = fits.get(ordered[0].get("id")) if ordered else None
    return ordered, {
        "profile_declared": bool(worker_profile),
        "ready": len(tasks),
        "compatible": len(compatible),
        "excluded": len(tasks) - len(compatible),
        "excluded_by_reason": dict(sorted(reasons.items())),
        "selected": ({"task": ordered[0].get("id"),
                      "outcome_score": round(selected["outcome_score"], 6),
                      "locality_score": round(selected["locality_score"], 6)}
                     if selected else None),
    }


def order_ready(tasks, flags=None, now=None, busy_touches=None):
    """Order the public canonical ready rail without assuming any particular worker profile."""
    ordered = pull_order(tasks, flags, now, busy_touches)
    if len(ordered) >= SATURATION_READY:
        kept = [task for task in ordered if not is_cosmetic(task)]
        if kept:
            return kept
    return ordered
