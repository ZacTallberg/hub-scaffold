"""MAST failure-mode tagging: every refusal this board records, named in one shared vocabulary.

The hub already refuses a great deal — 41 gate denials and 13 failing verifications on the
canonical board — but each refusal spoke only its own dialect (`acceptance_uncovered`,
`oracle:tamper:...`, exit 1). There was no way to ask "what kind of failure is this fleet having?",
which is the question that decides whether to fix a prompt, a gate, or a worker's role.

The vocabulary is the MAST 14-mode taxonomy (Cemri et al., "Why Do Multi-Agent LLM Systems Fail?",
NeurIPS 2025) across its three categories: specification, inter-agent misalignment, verification.

The classification is DETERMINISTIC and STRUCTURAL. A refusal is classified by WHICH GATE fired
and WHICH RULE it named — the gate's own typed fields — never by reading the message. Two lookups:

  * the write gates (active/complete) name a FIXED rule id, so (gate, rule) -> mode is a table;
  * the deploy gate names the first blocking AUDIT VIOLATION, whose id is unbounded
    (`schema:<project>:task:...`, `oracle:tamper:<task>:<sha>`), so it resolves by longest id PREFIX —
    the same discipline the guard baselines use, and for the same reason: an id prefix is a
    structural fact about which guard spoke, not a similarity score.

An unrecognized rule classifies to nothing. It is not guessed into the nearest mode: a wrong label
is worse than an absent one, because it looks like knowledge. The audit mirror raises it as amber
so the table gets extended by someone who knows which mode is right.
"""

# ---- the fixed 14-mode enum (order = the paper's) ----
SPECIFICATION = (
    "1.1 disobey task specification",
    "1.2 disobey role specification",
    "1.3 step repetition",
    "1.4 loss of conversation history",
    "1.5 unaware of termination conditions",
)
MISALIGNMENT = (
    "2.1 conversation reset",
    "2.2 fail to ask for clarification",
    "2.3 task derailment",
    "2.4 information withholding",
    "2.5 ignored other agent input",
    "2.6 reasoning-action mismatch",
)
VERIFICATION = (
    "3.1 premature termination",
    "3.2 no or incomplete verification",
    "3.3 incorrect verification",
)
MODES = SPECIFICATION + MISALIGNMENT + VERIFICATION
CATEGORY = {}
for _mode in SPECIFICATION:
    CATEGORY[_mode] = "specification"
for _mode in MISALIGNMENT:
    CATEGORY[_mode] = "misalignment"
for _mode in VERIFICATION:
    CATEGORY[_mode] = "verification"

# ---- (gate, rule) -> mode, for the write gates' FIXED rule ids ----
# Each entry answers: what did the agent do that the gate caught? Not what the gate is called.
GATE_RULES = {
    # complete(): the work was offered as finished when its own proof did not cover it.
    ("complete", "acceptance_uncovered"): "3.2 no or incomplete verification",
    ("complete", "evidence_unresolvable"): "3.2 no or incomplete verification",
    ("complete", "evidence_unrelated"): "3.2 no or incomplete verification",
    ("complete", "receipt_command_mismatch"): "3.3 incorrect verification",
    ("complete", "verification_vacuous"): "3.3 incorrect verification",
    # Stopping while the system is still unsound, or while a declared dep is unmet, is calling it
    # finished before the termination condition holds.
    ("complete", "deps_unmet"): "3.1 premature termination",
    ("complete", "poison_blocked"): "1.5 unaware of termination conditions",
    # The seat rules: one active per agent, your own lease, your own identity.
    ("active", "one_active"): "1.2 disobey role specification",
    ("active", "identity"): "1.2 disobey role specification",
    ("active", "lease_required"): "1.2 disobey role specification",
    ("active", "not_lease_holder"): "1.2 disobey role specification",
    ("complete", "identity"): "1.2 disobey role specification",
    ("complete", "not_lease_holder"): "1.2 disobey role specification",
    # A payload the vocabulary forbids, or a frozen field edited without a ruling: the task was
    # specified and the write did something else.
    ("active", "schema"): "1.1 disobey task specification",
    ("complete", "schema"): "1.1 disobey task specification",
    ("active", "scope_frozen"): "1.1 disobey task specification",
    ("complete", "scope_frozen"): "1.1 disobey task specification",
    ("active", "work_kind_frozen"): "1.1 disobey task specification",
}

# ---- audit violation id prefix -> mode, for gates that name a VIOLATION (deploy) ----
# Longest prefix wins, so a precise guard is never shadowed by a broad one.
VIOLATION_MODES = {
    "oracle:tamper": "3.3 incorrect verification",
    "receipt:command-mismatch": "3.3 incorrect verification",
    "receipt:subject-mismatch": "3.3 incorrect verification",
    "receipt:store-tamper": "3.3 incorrect verification",
    "verifier:self-graded": "3.3 incorrect verification",
    "coherence:reward-hacking-gap": "3.3 incorrect verification",
    "receipt:missing": "3.2 no or incomplete verification",
    "coverage:uncovered": "3.2 no or incomplete verification",
    "evidence:unrelated": "3.2 no or incomplete verification",
    "evidence:rot": "3.2 no or incomplete verification",
    "verification:untouched-change": "3.2 no or incomplete verification",
    "vc:vacuous": "3.2 no or incomplete verification",
    "unlanded:done": "3.1 premature termination",
    "deps:unmet": "3.1 premature termination",
    "task:uncommitted": "3.1 premature termination",
    "coherence:repo": "3.1 premature termination",
    "coherence:served": "3.1 premature termination",
    "schema:": "1.1 disobey task specification",
    "scope:changed": "1.1 disobey task specification",
    "scope:vc-changed": "1.1 disobey task specification",
    "dangling:": "1.1 disobey task specification",
    "one-active": "1.2 disobey role specification",
    "active-lease": "1.2 disobey role specification",
    "commit:untasked": "1.2 disobey role specification",
    "identity:literal-key": "1.2 disobey role specification",
    "memory:poisoned-mint": "2.4 information withholding",
    "coherence:worker-spin": "1.3 step repetition",
    "task:reverted": "1.5 unaware of termination conditions",
    "run:budget-breached": "1.5 unaware of termination conditions",
    "chain:tamper": "3.3 incorrect verification",
    "index:divergence": "3.3 incorrect verification",
    "index:count-divergence": "3.3 incorrect verification",
}
_VIOLATION_ORDER = sorted(VIOLATION_MODES, key=lambda p: (-len(p), p))


def mode_for_violation(vid):
    """The mode of the guard that emitted this violation id, by longest registered prefix."""
    vid = str(vid or "")
    for prefix in _VIOLATION_ORDER:
        if vid == prefix or vid.startswith(prefix):
            return VIOLATION_MODES[prefix]
    return None


def classify(gate, rule):
    """(gate, rule) -> a MAST mode, or None when the table does not know it.

    None is a real answer. Guessing the nearest mode would put a confident wrong label on a
    refusal nobody has looked at, and a taxonomy is only worth having if its labels are true."""
    if not rule:
        return None
    hit = GATE_RULES.get((gate, str(rule)))
    if hit:
        return hit
    # A gate that names a violation (deploy, and any future audit-driven refusal) resolves through
    # the guard that actually spoke.
    return mode_for_violation(rule)


def classify_event(ev):
    """The mode for one ledger event, or None when it is not a failure (or is unclassifiable).

    Two failure shapes exist on this board: a gate DENY, and a verification that exited nonzero —
    work that did not satisfy the very command it declared as its proof, which is the plainest
    'did not do what was specified' the system can observe."""
    etype = ev.get("type", "")
    payload = ev.get("payload") or {}
    if etype == "gate.decision":
        if payload.get("decision") != "deny":
            return None
        return classify(payload.get("gate"), payload.get("rule_id"))
    if etype == "verification.recorded" and payload.get("exit_code") not in (0, None):
        return "1.1 disobey task specification"
    return None


def is_failure(ev):
    """Whether an event is a refusal at all — the denominator the histogram and the guard share,
    so 'unclassified' can never mean 'not a failure'."""
    etype = ev.get("type", "")
    payload = ev.get("payload") or {}
    if etype == "gate.decision":
        return payload.get("decision") == "deny"
    return etype == "verification.recorded" and payload.get("exit_code") not in (0, None)


def histogram(events):
    """{modes: {mode: n}, categories: {category: n}, total, unclassified: [(seq, gate, rule)]}."""
    modes, categories, unclassified, total = {}, {}, [], 0
    for ev in events:
        if not is_failure(ev):
            continue
        total += 1
        mode = classify_event(ev)
        if mode is None:
            payload = ev.get("payload") or {}
            unclassified.append({"seq": ev.get("seq"), "gate": payload.get("gate"),
                                 "rule": payload.get("rule_id")})
            continue
        modes[mode] = modes.get(mode, 0) + 1
        cat = CATEGORY[mode]
        categories[cat] = categories.get(cat, 0) + 1
    return {"total": total, "modes": modes, "categories": categories,
            "unclassified": unclassified}
