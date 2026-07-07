"""The stack-neutral audit core: computed-not-attested, tamper-evident, fail-closed.

Emits a structured violations[] list + an OPA-style exit code: 0=PASS, 2=CRITICAL/HIGH (blocks
deploy), 3=WARN-only (amber), 1=internal-error (fail-closed RED). NEVER trusts a stored boolean.

Stack-specific behavioral checks (route-introspection for unguarded mutations, AST settings safety,
the out-of-band live probe) plug in as `adapters`: callables(state) -> list[violation].
"""
from .store import EventStore


def _v(vid, severity, invariant, observed, expected, *, kind="logic", remediation="", autofix_allowed=False):
    return {
        "id": vid, "kind": kind, "severity": severity, "status": "open",
        "invariant": invariant, "observed": observed, "expected": expected,
        "evidence_uri": "", "remediation": remediation, "autofix_allowed": autofix_allowed,
    }


def audit(state, registry, *, store: EventStore = None, coherence: dict = None, adapters=None) -> dict:
    """Compute the audit. `state` from project.state(); `coherence` is {repo,deploy,sha,head,served,...}
    computed by the caller (git HEAD vs state.sha vs live served_sha)."""
    violations = []
    entities = state["entities"]

    # 1. every entity validates against its schema (the if/then rules make false claims fail here:
    #    done->verified_by, blocked->deps, shipped-feat->tasks, superseded-adr->successor)
    from .validate import validate as _validate
    for eid, ent in entities.items():
        et = ent.get("type")
        for err in _validate(ent, et, registry):
            violations.append(_v(f"schema:{eid}", "high", "entity validates against its schema",
                                 f"{eid}: {err}", "valid per hub:%s" % et, kind="schema",
                                 remediation="fix the entity payload"))

    # 2. referential integrity: no dangling idref
    for dgl in state.get("dangling", []):
        violations.append(_v(f"dangling:{dgl['from']}->{dgl['to']}", "high",
                             "every idref resolves to a real entity",
                             f"{dgl['from']} {dgl['rel']} -> {dgl['to']} (missing)", "target exists",
                             kind="logic", remediation="create the target or fix the reference"))

    # 3. ADR gap-free numbering
    nums = sorted(a.get("number") for a in state["by_type"].get("adr", []) if isinstance(a.get("number"), int))
    for i, n in enumerate(nums, start=1):
        if n != i:
            violations.append(_v("adr:numbering", "warn", "ADR numbers are gap-free + contiguous",
                                 f"found {n} at position {i}", f"{i}", kind="logic"))
            break

    # 4. tamper-evidence: the hash-chain verifies
    if store is not None:
        chain = store.verify_chain()
        if not chain["ok"]:
            violations.append(_v("chain:tamper", "critical", "event hash-chain is intact",
                                 "; ".join(chain["errors"][:3]), "chain verifies", kind="logic"))

    # 5. RECOMPUTE-NOT-TRUST build coherence (the classic false-green killer)
    if coherence is not None:
        head, sha, served = coherence.get("head"), coherence.get("sha"), coherence.get("served")
        if head and sha and head != sha:
            violations.append(_v("coherence:repo", "high", "state.last_deploy_sha == git HEAD",
                                 f"sha={sha} head={head}", "equal", kind="probe",
                                 remediation="redeploy or reconcile the deploy record"))
        if served and head and served != head:
            violations.append(_v("coherence:served", "high", "live served_sha == git HEAD",
                                 f"served={served} head={head}", "equal", kind="probe",
                                 remediation="the live artifact is stale; redeploy"))
        if coherence.get("unknown"):
            violations.append(_v("coherence:unknown", coherence.get("unknown_severity", "high"),
                                 "coherence is knowable (not stale/unreachable)",
                                 coherence.get("unknown"), "known", kind="probe"))

    # 6. stack-specific behavioral adapters (route auth, settings AST, live probe)
    for ad in (adapters or []):
        try:
            violations.extend(ad(state) or [])
        except Exception as e:  # an adapter that errors is FAIL-CLOSED, never silently green
            violations.append(_v("adapter:error", "critical", "every audit adapter runs",
                                 f"{getattr(ad,'__name__',ad)}: {e}", "adapter completes", kind="logic"))

    sev = {v["severity"] for v in violations}
    if "critical" in sev or "high" in sev:
        exit_code, ok = 2, False
    elif "warn" in sev:
        exit_code, ok = 3, True       # amber: non-blocking
    else:
        exit_code, ok = 0, True
    return {
        "ok": ok,
        "exit_code": exit_code,
        "violations": violations,
        "counts": {"critical": sum(s == "critical" for s in (v["severity"] for v in violations)),
                   "high": sum(v["severity"] == "high" for v in violations),
                   "warn": sum(v["severity"] == "warn" for v in violations)},
    }
