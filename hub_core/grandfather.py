"""Grandfather baselines: a guard does not indict history it was not there to watch.

A guard wired at ledger seq N has no standing over a condition that was already frozen into the
ledger before N. The worker who could have acted is gone, the event is immutable, and an amber
nobody can clear is exactly how an operator learns to ignore the whole rail — the failure mode
that buries the ONE warn that matters. This module silences that class and only that class.

Three fences keep ordinary guard silencing from becoming a vacuous green:

  * SEVERITY — only `warn` is ever grandfathered. A critical or high is a claim about the PRESENT
    (the chain is broken, master lacks the work); no baseline may touch one.
  * SUBJECT — a violation is grandfathered only when it names a `subject` aggregate whose
    condition seq resolves in the ledger. No subject, no silencing — never a text guess.
  * ACCOUNTING — suppression returns what it dropped. The caller reports the count; a rail that
    silently shrinks is worse than a noisy one.

The CONDITION seq of an aggregate is the seq of the event that established its current status
(its birth seq when it never carries one) — the moment the violating fact became true, which is
what a baseline must be compared against. A pre-gate `done` is anchored at the completion that
granted it, while a task reverted out of `done` last hour anchors at that recent reversion, so a
legacy completion goes quiet and a fresh anomaly stays loud. The baseline seq itself belongs to
the guard: a condition established AT it happened with the guard already present.

There is one deliberately narrower migration lane beside those ordinary warn baselines: an adopter
may anchor the ledger head at which strict done-task receipts arrived. Only a task already `done` at
or before that exact hash-anchored cutoff, and invalid *solely* because `verified_by` or
`evidence_uri` is absent/empty, is accounted as legacy receipt debt. The historical entity is never
rewritten and no evidence is invented. Anything completed later, any mismatched anchor, or any
additional schema defect remains a blocking high finding.

An equally narrow adopter-schema lane may account for immutable pre-adoption entities that carried
extensions outside today's canonical schemas. It is tied to that same original receipt cutoff and
allows only an exact entity-type + portable validation signature whose subject was never touched
after the cutoff. A later write therefore opts the whole entity into the current contract.
"""

import re


LEGACY_ENTITY_SCHEMA_POLICY = (
    "pre-cutoff untouched entities with exact portable schema signatures only"
)


def condition_seq(events):
    """{aggregate: seq of the event that established its CURRENT status}. Aggregates that never
    carry a status (commits, notes) anchor at their first event — the moment the record froze."""
    seqs, current = {}, {}
    for ev in events:
        agg = ev.get("aggregate")
        if not agg:
            continue
        seq = ev.get("seq") or 0
        if agg not in seqs:
            seqs[agg] = seq
        status = (ev.get("payload") or {}).get("status")
        if status is not None and current.get(agg) != status:
            current[agg] = status
            seqs[agg] = seq
    return seqs


def anchors_at(events, seqs):
    """{seq: event hash} for exactly the seqs asked for — the chain points the baselines name."""
    want = set(seqs)
    return {ev["seq"]: ev.get("hash") for ev in events
            if ev.get("seq") in want}


def last_event_seq(events):
    """{aggregate: last canonical event seq}; every post-cutoff touch remains visible."""
    seqs = {}
    for event in events:
        aggregate = event.get("aggregate")
        seq = event.get("seq")
        if aggregate and isinstance(seq, int):
            seqs[aggregate] = seq
    return seqs


def legacy_receipt_context(events, baseline):
    """Return the anchored cutoff and current-condition seqs, or None fail-closed.

    This is intentionally not a general high-severity suppressor. The audit owns the sole allowed
    shape (legacy done receipts) and uses this helper only to prove that the cutoff belongs to this
    exact immutable ledger.
    """
    if not isinstance(baseline, dict):
        return None
    seq = baseline.get("seq")
    anchor = baseline.get("anchor_hash")
    if not isinstance(seq, int) or seq <= 0 or not isinstance(anchor, str) or not anchor:
        return None
    events = list(events)
    if anchors_at(events, [seq]).get(seq) != anchor:
        return None
    return {"seq": seq, "anchor_hash": anchor, "conditions": condition_seq(events)}


def legacy_entity_schema_context(events, baseline, receipt_baseline):
    """Return a fail-closed exact schema-compatibility context, or ``None``.

    The cutoff is not independently selectable: it must be the adopter's original
    ``legacy_done_receipts`` anchor.  Signatures are accepted only in their canonical hash form.
    """
    if not isinstance(baseline, dict) or not isinstance(receipt_baseline, dict):
        return None
    seq = baseline.get("seq")
    anchor = baseline.get("anchor_hash")
    if seq != receipt_baseline.get("seq") or anchor != receipt_baseline.get("anchor_hash"):
        return None
    if (
        not isinstance(seq, int)
        or seq <= 0
        or not isinstance(anchor, str)
        or not anchor
        or not isinstance(baseline.get("captured_at"), str)
        or not baseline.get("captured_at")
        or baseline.get("policy") != LEGACY_ENTITY_SCHEMA_POLICY
    ):
        return None
    raw = baseline.get("signatures")
    if not isinstance(raw, dict):
        return None
    signatures = {}
    for entity_type, values in raw.items():
        if not isinstance(entity_type, str) or not entity_type or not isinstance(values, list):
            return None
        if any(
            not isinstance(value, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
            for value in values
        ):
            return None
        if len(values) != len(set(values)):
            return None
        signatures[entity_type] = frozenset(values)
    events = list(events)
    if anchors_at(events, [seq]).get(seq) != anchor:
        return None
    return {
        "seq": seq,
        "anchor_hash": anchor,
        "signatures": signatures,
        "last_events": last_event_seq(events),
    }


def baseline_index(guards, baselines, anchors=None):
    """[(violation-id prefix, baseline seq, guard name)], longest prefix first.

    `guards` are registry-shaped ({name, ids}); `baselines` is {guard name: {"seq", "anchor_hash"}}.
    A guard with no recorded baseline is simply absent — it grandfathers nothing, the fail-closed
    direction (it keeps firing over everything, exactly as it did before).

    A seq alone is meaningless off its own ledger: seq 199 on a fresh temp board is a different
    moment entirely, and applying the canonical board's numbers there would silence a guard's
    whole fixture. So a baseline also names the HASH of the event at that seq, and `anchors` is
    what this board actually carries there. A baseline whose anchor is missing or mismatched does
    not apply — which also means a rewritten prefix disables the baselines rather than quietly
    widening what they hide."""
    index = []
    for g in guards:
        rec = baselines.get(g["name"])
        if not rec:
            continue
        seq = rec.get("seq")
        if not isinstance(seq, int) or seq <= 0:
            continue
        if anchors is not None:
            anchor = rec.get("anchor_hash")
            if not anchor or anchors.get(seq) != anchor:
                continue
        for gid in g["ids"]:
            index.append((gid, seq, g["name"]))
    index.sort(key=lambda e: (-len(e[0]), e[0]))
    return index


def _baseline_for(vid, index):
    """The baseline of the MOST SPECIFIC guard whose id prefixes this violation (the index is
    longest-first), so a broad core id never shadows the precise guard that owns the finding."""
    for gid, seq, name in index:
        if vid == gid or vid.startswith(gid):
            return seq, name
    return None, None


def partition(violations, index, conditions):
    """(kept, suppressed) — suppressed are the warns whose subject's condition predates the
    baseline of the guard that emitted them. Everything else is kept, untouched."""
    kept, suppressed = [], []
    for v in violations:
        if v.get("severity") != "warn":
            kept.append(v)
            continue
        subject = v.get("subject")
        cond = conditions.get(subject) if subject else None
        if cond is None:
            kept.append(v)
            continue
        baseline, guard = _baseline_for(v.get("id", ""), index)
        if baseline is not None and cond < baseline:
            suppressed.append(dict(v, grandfathered={"guard": guard, "baseline_seq": baseline,
                                                     "condition_seq": cond}))
        else:
            kept.append(v)
    return kept, suppressed


def suppressor(events, guards, baselines):
    """Bind one audit pass: returns fn(violations) -> (kept, suppressed), or None when nothing
    applies to THIS board — no baselines recorded, or none whose anchor this ledger carries — so
    the audit runs exactly as it always did."""
    events = list(events)
    anchors = anchors_at(events, (r.get("seq") for r in baselines.values()
                                  if isinstance(r, dict) and isinstance(r.get("seq"), int)))
    index = baseline_index(guards, baselines, anchors)
    if not index:
        return None
    conditions = condition_seq(events)

    def _suppress(violations):
        return partition(violations, index, conditions)

    return _suppress
