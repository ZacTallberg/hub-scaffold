"""Bias-controlled, calibrated judge for the borderline-confirm path.

Deterministic signals DECIDE; a judge only CONFIRMS a borderline the deterministic layer could
not settle (SLM-first law: string similarity never decides identity). Two controls make that
confirmation trustworthy (JUDGe'26 / position-bias literature):

  1. POSITION-SWAP INVARIANCE — the judge scores (a, b) AND (b, a) and the verdict comes from
     the swap-average, so ordering can never flip a confirm. The measured |gap| between the two
     orderings rides on the verdict as `position_bias`.
  2. CALIBRATION FLOOR (fail-closed) — a judge must first prove agreement >= floor against a
     LABELED borderline set the deterministic layer already ruled (`calibrate`). No receipt, a
     stale-shaped receipt, or a below-floor receipt BLOCKS the confirm and raises an alarm;
     it never silently confirms.

The judge callable is injected (`judge_fn(a, b) -> score in [0..1]`), so the control structure
is provable deterministically and any scorer — SLM, LLM, heuristic — plugs in unchanged.
"""

DEFAULT_FLOOR = 0.8
CONFIRM_THRESHOLD = 0.5


def _swap_scores(judge_fn, a, b):
    s_ab = float(judge_fn(a, b))
    s_ba = float(judge_fn(b, a))
    for s in (s_ab, s_ba):
        if not 0.0 <= s <= 1.0:
            raise ValueError(f"judge_fn must score in [0..1], got {s!r}")
    return s_ab, s_ba


def calibrate(judge_fn, labeled, *, floor=DEFAULT_FLOOR):
    """Score the judge against pairs the deterministic layer already ruled.

    labeled: iterable of (a, b, expected_bool). Returns the calibration receipt
    {n, agreement, floor, passed} — the credential judge_confirm requires. Verdicts are
    swap-averaged here too: the judge is calibrated exactly as it will be used."""
    labeled = list(labeled)
    if not labeled:
        return {"n": 0, "agreement": 0.0, "floor": floor, "passed": False}
    hits = 0
    for a, b, expected in labeled:
        s_ab, s_ba = _swap_scores(judge_fn, a, b)
        if (((s_ab + s_ba) / 2.0) >= CONFIRM_THRESHOLD) == bool(expected):
            hits += 1
    agreement = hits / len(labeled)
    return {"n": len(labeled), "agreement": agreement, "floor": floor,
            "passed": agreement >= floor}


def judge_confirm(a, b, judge_fn, *, calibration, floor=None):
    """One bias-controlled confirmation of a borderline pair.

    Returns {confirmed, blocked, score, position_bias, agreement, alarm}. Order-swap invariant
    by construction: judge_confirm(a, b, ...) and judge_confirm(b, a, ...) carry the same
    verdict. Fail-closed: a missing/failed/below-floor calibration BLOCKS (confirmed False,
    blocked True, alarm set) — an uncalibrated judge never confirms anything."""
    if floor is None:
        floor = calibration.get("floor", DEFAULT_FLOOR) if isinstance(calibration, dict) else DEFAULT_FLOOR
    if not isinstance(calibration, dict) or not calibration.get("n"):
        return {"confirmed": False, "blocked": True, "score": None, "position_bias": None,
                "agreement": None,
                "alarm": "uncalibrated judge: no calibration receipt — confirm blocked (fail-closed)"}
    agreement = float(calibration.get("agreement", 0.0))
    if agreement < floor:
        return {"confirmed": False, "blocked": True, "score": None, "position_bias": None,
                "agreement": agreement,
                "alarm": f"judge agreement {agreement:.2f} below floor {floor:.2f} on the labeled "
                         "borderline set — confirm blocked (fail-closed)"}
    s_ab, s_ba = _swap_scores(judge_fn, a, b)
    score = (s_ab + s_ba) / 2.0
    return {"confirmed": score >= CONFIRM_THRESHOLD, "blocked": False, "score": score,
            "position_bias": abs(s_ab - s_ba), "agreement": agreement, "alarm": None}
