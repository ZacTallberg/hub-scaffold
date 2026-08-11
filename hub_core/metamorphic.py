"""Metamorphic relations over the ledger: properties that must hold BETWEEN two runs.

An ordinary check asks "is this output right?" and needs a known answer. A metamorphic relation
asks "do these two runs agree?" and needs none — which is why it catches the corruption class no
oracle on this board can see: a fold that is subtly order-sensitive, a fold that mutates the very
events it was handed, an incremental fast path that has drifted from the full computation it
replaces. Each of those produces a perfectly well-formed, perfectly wrong answer, and every
existing guard would read it as green.

All three have real history here. The fold once aliased provenance-carrying events and mutated
them in place, so a second fold of the same list disagreed with the first and a false-critical
refused every worker's completion. verify_chain's incremental checkpoint exists to keep a warm
audit cheap, and an incremental path that silently diverges from the full replay is a
tamper-evidence system certifying bytes it never re-hashed.

CRITICAL, not amber: a broken relation means the board's own arithmetic disagrees with itself.
"""
from .canonical import canonical
from .store import sha256_hex


def snapshot_hash(state):
    """A content fingerprint of a folded state's entities, canonical and order-independent, so
    two folds are compared by WHAT they concluded rather than by dict insertion order."""
    entities = (state or {}).get("entities", {})
    return sha256_hex(canonical({k: entities[k] for k in sorted(entities)}))


def valid_permutation(events):
    """A different interleaving of the SAME events that any correct fold must agree with.

    Per-aggregate order is preserved — that ordering is causal, and reordering it would change the
    answer legitimately (last-write-wins). What is permuted is the interleaving BETWEEN aggregates,
    which nothing in the fold's semantics may depend on. Deterministic (a stable round-robin over
    aggregates in reverse first-appearance order), so a violation reproduces exactly rather than
    appearing one run in ten."""
    streams, order = {}, []
    for ev in events:
        agg = ev.get("aggregate") or "\x00none"
        if agg not in streams:
            streams[agg] = []
            order.append(agg)
        streams[agg].append(ev)
    order.reverse()
    out, cursors = [], {agg: 0 for agg in order}
    remaining = sum(len(s) for s in streams.values())
    while remaining:
        for agg in order:
            i = cursors[agg]
            if i < len(streams[agg]):
                out.append(streams[agg][i])
                cursors[agg] = i + 1
                remaining -= 1
    return out


def relations(events, fold, verify_full=None, verify_incremental=None):
    """Evaluate every relation. Returns [{name, ok, detail}] — one entry per relation, always,
    so a relation that could not be evaluated is visible rather than absent."""
    events = list(events)
    results = []

    # R1 — PERMUTATION INVARIANCE: the terminal state does not depend on how independent
    #      aggregates were interleaved.
    base = snapshot_hash(fold(events))
    permuted = snapshot_hash(fold(valid_permutation(events)))
    results.append({
        "name": "fold-permutation-invariant",
        "ok": base == permuted,
        "detail": ("terminal snapshot identical under a valid re-interleaving"
                   if base == permuted else
                   f"a valid permutation folded to {permuted[:12]}, not {base[:12]} — the fold "
                   "depends on the interleaving of independent aggregates"),
    })

    # R2 — APPEND-THEN-REVERT: folding a superset and then the original again returns the original
    #      answer. Only fails when a fold leaves a trace behind it — a mutated input event, cached
    #      state that outlives the call — which is exactly the aliasing outage's signature.
    probe = dict(events[-1]) if events else {"aggregate": "\x00probe", "seq": 1, "payload": {}}
    probe = dict(probe, seq=(probe.get("seq") or 0) + 1_000_000, aggregate="\x00metamorphic-probe",
                 payload=dict(probe.get("payload") or {}))
    fold(events + [probe])
    after = snapshot_hash(fold(events))
    results.append({
        "name": "fold-append-revert-idempotent",
        "ok": after == base,
        "detail": ("re-folding the original events returns the pre-append snapshot"
                   if after == base else
                   f"after folding a superset, the ORIGINAL events fold to {after[:12]}, not "
                   f"{base[:12]} — the fold mutated its input or kept state between calls"),
    })

    # R3 — INCREMENTAL == FULL: the warm chain verification agrees with the cold replay it exists
    #      to avoid. A fast path that has drifted is tamper-evidence certifying unread bytes.
    if verify_full and verify_incremental:
        full, inc = verify_full(), verify_incremental()
        agree = (full.get("ok"), full.get("count")) == (inc.get("ok"), inc.get("count"))
        results.append({
            "name": "chain-incremental-equals-full",
            "ok": agree,
            "detail": ("incremental verify agrees with the full replay "
                       f"(ok={full.get('ok')}, {full.get('count')} events)" if agree else
                       f"incremental says ok={inc.get('ok')} over {inc.get('count')} events; the "
                       f"full replay says ok={full.get('ok')} over {full.get('count')}"),
        })
    else:
        results.append({"name": "chain-incremental-equals-full", "ok": None,
                        "detail": "not evaluated: no chain verifier supplied in this context"})
    return results
