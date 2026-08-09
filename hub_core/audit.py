"""The stack-neutral audit core: computed-not-attested, tamper-evident, fail-closed.

Emits a structured violations[] list + an OPA-style exit code: 0=PASS, 2=CRITICAL/HIGH (blocks
deploy), 3=WARN-only (amber), 1=internal-error (fail-closed RED). NEVER trusts a stored boolean.

Stack-specific behavioral checks (route-introspection for unguarded mutations, AST settings safety,
the out-of-band live probe) plug in as `adapters`: callables(state) -> list[violation].
"""
from .store import EventStore


import contextlib as _contextlib
import json as _json
import os as _os
import time as _time
import uuid as _uuid

# The declared deadlines, sized from a MEASURED live run rather than picked. A full audit measured
# 92.4s (python tools/audit.py, post ember:task:0506/0514), well inside AUDIT_DEADLINE_S.
#
# THE PER-ADAPTER BOUND-SETTER CHANGES AS ADAPTERS GET FIXED (ember:adr:0015, amended
# ember:task:0525) — read this as "which adapter sets 180s today", not as a fact about any one
# adapter. Two have already been fixed in turn: oracle_tamper_adapter (cold 56s -> ~2.85s,
# ember:task:0506) and receipt_subject_adapter (cold 57-118s -> 0.20-1.16s, ember:task:0521), both
# by the same move — batch the git calls behind a persistent per-repo cache keyed on something
# immutable (a commit's diff, a commit's tree). Neither can regress silently: each fix shipped a
# spawn-count guard, not a stopwatch, so a re-introduced per-commit shell-out is caught structurally.
#
# The current leaders are CACHE-BOUND, same shape as the two already fixed — expensive once, then
# near-free on a repeat run — and nowhere close to threatening the bound: evidence_relatedness_adapter
# (cold 25-28s, warm 0.18-0.24s), evidence_rot_adapter (cold 23-32s, warm ~0s), verification_touch_adapter
# (cold 19-23s, warm 0.01-0.05s). The worst observed cold cost sits at better than 5.5x headroom under
# 180s — comfortable, so no fix is tracked for these today.
#
# A deadline tuned to the warm path fires on the operator whose cache is empty, which is exactly the
# operator running a release audit on a fresh checkout. A ceiling under the healthy total would be
# worse than none: it would report INCONCLUSIVE forever on a perfectly good board, and a verdict that
# never varies carries no information.
ADAPTER_DEADLINE_S = float(_os.environ.get("HUB_AUDIT_ADAPTER_DEADLINE_S") or 180)

# The share of the per-adapter deadline a COLD adapter run may spend. Declared, so the answer to "the
# audit is slow" has to be a fix rather than a bigger number: raising ADAPTER_DEADLINE_S raises this
# budget in the same proportion, and the guard that measures it goes red either way.
ADAPTER_COLD_BUDGET_FRACTION = 0.25
AUDIT_DEADLINE_S = float(_os.environ.get("HUB_AUDIT_DEADLINE_S") or 600)


import threading as _threading

_REAPER_LOCK = _threading.RLock()


@_contextlib.contextmanager
def _child_reaper():
    """Kill any process an adapter spawned and left running.

    Abandoning the worker thread on timeout is safe for the audit but not for the machine: the
    adapters shell out to git, and a hung adapter's `git` was surviving the run that gave up on it.
    Popen is wrapped for the duration so every child is known and can be killed on the way out.
    """
    import subprocess as _sp
    spawned = []

    # Popen is a MODULE GLOBAL, so two audits running at once would each see the other's children
    # and could kill a git process the other run is still reading. Serialised for the patched window;
    # the hot request lanes serve from the last-audit cache rather than recomputing, so the audits
    # that contend here are the heavyweight ones that were never concurrent-friendly anyway.
    with _REAPER_LOCK:
        original = _sp.Popen

        class _Tracked(original):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                spawned.append(self)

        _sp.Popen = _Tracked
        try:
            yield spawned
        finally:
            _sp.Popen = original
            for child in spawned:
                if child.poll() is None:
                    try:
                        child.kill()
                        child.wait(timeout=5)
                    except Exception:
                        pass


def _v(vid, severity, invariant, observed, expected, *, kind="logic", remediation="",
       autofix_allowed=False, subject=""):
    """`subject` names the aggregate the violation is ABOUT (a task/commit id) — the deterministic
    handle the grandfather baselines resolve, so no consumer ever has to parse it back out of the
    id. Board-wide probes (freshness, coherence) legitimately have none."""
    return {
        "id": vid, "kind": kind, "severity": severity, "status": "open",
        "invariant": invariant, "observed": observed, "expected": expected,
        "evidence_uri": "", "remediation": remediation, "autofix_allowed": autofix_allowed,
        "subject": subject,
    }


# The one file a deploy writes AFTER shipping: the deploy record itself. It is never part of
# the served artifact, so a commit touching only this is bookkeeping, not unshipped work.
DEPLOY_BOOKKEEPING_PATHS = frozenset({"PROJECT/state.json"})


def audit(state, registry, *, store: EventStore = None, coherence: dict = None, adapters=None,
          suppress=None) -> dict:
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
                                 f"found {n} at position {i}", f"{i}", kind="logic",
                                 remediation=f"renumber the ADR at position {i} to {i}, or record "
                                             "the missing ADR that would fill the gap"))
            break

    # 4. tamper-evidence: the hash-chain verifies
    if store is not None:
        chain = store.verify_chain()
        if not chain["ok"]:
            violations.append(_v("chain:tamper", "critical", "event hash-chain is intact",
                                 "; ".join(chain["errors"][:3]), "chain verifies", kind="logic"))

    # 5. RECOMPUTE-NOT-TRUST build coherence (the observed false-green killer)
    if coherence is not None:
        head, sha, served = coherence.get("head"), coherence.get("sha"), coherence.get("served")
        if head and sha and head != sha:
            # A deploy is RECORDED after it ships (state.last_deploy_sha is written once the canary
            # passes), so HEAD sits one commit past the shipped sha from then until the next deploy.
            # Demanding exact equality made this permanently high — green only in the instant
            # between shipping and recording. Quiet when the WHOLE delta is that bookkeeping file
            # (it is never served, so nothing about the live artifact is stale); anything else in
            # the range and the finding stands. A None delta means the range could not be read,
            # which is unknown, not empty — report it rather than assuming either way.
            delta = coherence.get("delta_paths")
            bookkeeping_only = delta is not None and bool(delta) and set(delta).issubset(
                DEPLOY_BOOKKEEPING_PATHS)
            if not bookkeeping_only:
                extra = ""
                if delta is None:
                    extra = " (could not read the commit range — treating as unshipped work)"
                elif delta:
                    extra = " (unshipped: %s)" % ", ".join(list(delta)[:4])
                violations.append(_v("coherence:repo", "high", "state.last_deploy_sha == git HEAD",
                                     f"sha={sha} head={head}{extra}", "equal", kind="probe",
                                     remediation="redeploy or reconcile the deploy record"))
        if served and head and served != head:
            violations.append(_v("coherence:served", "high", "live served_sha == git HEAD",
                                 f"served={served} head={head}", "equal", kind="probe",
                                 remediation="the live artifact is stale; redeploy"))
        elif head and served is None:
            # An UNPROBED live check must be visible, never assumed green: amber says "staleness
            # unmeasured in this context", which is a different fact from "measured and equal".
            violations.append(_v("coherence:served-unmeasured", "warn",
                                 "the live served artifact is probed, not assumed",
                                 f"head={head} known but served_sha was not probed in this context",
                                 "served probed", kind="probe",
                                 remediation="run the audit with the live probe (served=...) where liveness matters"))
        if coherence.get("unknown"):
            violations.append(_v("coherence:unknown", coherence.get("unknown_severity", "high"),
                                 "coherence is knowable (not stale/unreachable)",
                                 coherence.get("unknown"), "known", kind="probe",
                                 remediation=coherence.get("unknown_remediation")
                                 or "restore the build identity (a readable .git or build_sha.txt) "
                                    "and deploy, so the ledger and the running artifact can be compared"))

    # 6. stack-specific behavioral adapters (route auth, settings AST, live probe)
    #
    # Under a DEADLINE. An adapter that raised was already fail-closed, but one that simply never
    # returned hung the whole release audit — the operator saw no verdict at all, which is the worst
    # of the three answers because it is indistinguishable from "still working".
    #
    # A timeout is a NAMED release-blocking finding, never an omitted check: an audit that quietly
    # skips the probe it could not finish reads exactly like an audit that ran it and found nothing.
    adapter_report = {"ran": [], "timed_out": [], "not_started": []}
    deadline = _time.monotonic() + AUDIT_DEADLINE_S
    with _child_reaper() as _children:
        for ad in (adapters or []):
            name = getattr(ad, "__name__", None) or repr(ad)
            left = deadline - _time.monotonic()
            if left <= 0:
                # Not started is its own fact, distinct from ran-and-clean and from timed-out.
                adapter_report["not_started"].append(name)
                violations.append(_v(
                    f"adapter:unrun:{name}", "critical", "every audit adapter runs",
                    f"{name} never started: the {AUDIT_DEADLINE_S:.0f}s audit deadline was already "
                    f"spent by earlier adapters", "adapter completes", kind="probe",
                    remediation="fix the adapter that consumed the budget, then re-run the audit"))
                continue
            budget = min(ADAPTER_DEADLINE_S, left)
            # A DAEMON thread, deliberately, and not a ThreadPoolExecutor. The pool's workers are
            # non-daemon and concurrent.futures joins every one of them at interpreter exit, so
            # abandoning a hung adapter there just moves the hang: the audit returned its verdict on
            # time and then the process would not exit. A bound that only holds until you try to
            # leave is not a bound. A daemon thread can be walked away from for good.
            box = {}

            def _runner(_ad=ad):
                try:
                    box["value"] = _ad(state) or []
                except BaseException as exc:                      # noqa: BLE001 - fail-closed
                    box["error"] = exc

            worker = _threading.Thread(target=_runner, name=f"audit-{name}", daemon=True)
            worker.start()
            worker.join(budget)

            if worker.is_alive():
                adapter_report["timed_out"].append(name)
                violations.append(_v(
                    f"adapter:timeout:{name}", "critical",
                    "every audit adapter answers within its deadline",
                    f"{name} did not return within {budget:.0f}s", "adapter completes", kind="probe",
                    remediation="the adapter is blocked (a hung probe, subprocess or network call); "
                                "bound the call it makes, then re-run the audit"))
            elif "error" in box:  # an adapter that errors is FAIL-CLOSED, never silently green
                adapter_report["ran"].append(name)
                violations.append(_v("adapter:error", "critical", "every audit adapter runs",
                                     f"{name}: {box['error']}", "adapter completes", kind="logic"))
            else:
                violations.extend(box.get("value") or [])
                adapter_report["ran"].append(name)

    # 7. grandfather baselines (hub_core.grandfather): drop the warns whose subject was already
    #    frozen into the ledger before the guard that indicts it existed. Applied HERE so counts
    #    and the exit code describe the rail the operator actually reads — and returned, never
    #    discarded, so a shrinking rail is always accounted for.
    suppressed = []
    if suppress is not None:
        violations, suppressed = suppress(violations)
    ledger = {"count": len(suppressed), "by_guard": {}, "silenced": []}
    for v in suppressed:
        gf = v.get("grandfathered") or {}
        guard = gf.get("guard") or "?"
        ledger["by_guard"][guard] = ledger["by_guard"].get(guard, 0) + 1
        ledger["silenced"].append({"id": v.get("id"), "guard": guard,
                                   "baseline_seq": gf.get("baseline_seq"),
                                   "condition_seq": gf.get("condition_seq")})

    sev = {v["severity"] for v in violations}
    if "critical" in sev or "high" in sev:
        exit_code, ok = 2, False
    elif "warn" in sev:
        exit_code, ok = 3, True       # amber: non-blocking
    else:
        exit_code, ok = 0, True

    # THREE answers, not two. A run that could not finish its checks is INCONCLUSIVE — blocking like
    # a FAIL, because an unfinished audit is no licence to release, but named differently so nobody
    # goes hunting for a defect that was never actually observed.
    incomplete = adapter_report["timed_out"] + adapter_report["not_started"]
    verdict = "INCONCLUSIVE" if incomplete else ("PASS" if ok else "FAIL")
    return {
        "ok": ok,
        "exit_code": exit_code,
        "verdict": verdict,
        "adapters": adapter_report,
        "violations": violations,
        "grandfathered": ledger,
        "counts": {"critical": sum(s == "critical" for s in (v["severity"] for v in violations)),
                   "high": sum(v["severity"] == "high" for v in violations),
                   "warn": sum(v["severity"] == "warn" for v in violations),
                   "grandfathered": len(suppressed)},
    }


# ---- oracle-tamper: the diff behind a receipt must not weaken the oracle it satisfied ----
# A worker's exit-0 receipt proves its verification_command PASSED — it proves nothing about
# whether the commit that "did the work" also weakened that very command's tests. This adapter
# parses the diff of every commit linked to a done code task and fires CRITICAL when the commit
# net-removed assertions, added skip/xfail marks, patched the oracle (mock/monkeypatch), or added
# an exit-0 short-circuit. Scope is two-ringed: vc-named files (any of .py/.js/.ts/.sh — full
# signal set) plus the commit-touched test surface, test_*.{py,js,ts,sh} (assert-removal, skip,
# exit-0: gutting or silencing a battery member games the deploy gate even when the task's own
# vc stays green; mock-adding stays vc-only because mocks in unrelated test work are authorship).
# A file-pair is in scope on its OLD or NEW path, so renaming or deleting an oracle never drops
# it from scope, and a "new" file whose path existed before the commit (rename-recreate) is an
# EDIT, not authorship. All checks are DETERMINISTIC line-pattern counts over the diff — never
# fuzzy similarity. The exit-0 receipt gate itself is untouched; this is the audit mirror.

import re as _re

_ORACLE_DEF_NAME = _re.compile(r"^\s*def\s+([A-Za-z_]\w*)\s*\(")
_ORACLE_CALL = _re.compile(r"\b([A-Za-z_]\w*)\s*\(")


def asserting_helpers(source):
    """Names of functions in one oracle file whose OWN body contains an assertion.

    Calling one of these is asserting — the assertion simply lives one frame down. Extracting the
    set is plain indentation scanning (a def's body is what is indented past its header), not a
    similarity judgement: a name is in this set or it is not."""
    names, current, header_indent = set(), None, 0
    for line in (source or "").splitlines():
        m = _ORACLE_DEF_NAME.match(line)
        if m:
            current, header_indent = m.group(1), len(line) - len(line.lstrip())
            continue
        if current is None or not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= header_indent:
            current = None                       # dedented out of the body
            continue
        if _ORACLE_ASSERT.match(line):
            names.add(current)
            current = None                       # one hit settles this def
    return names


_ORACLE_VC_PATH = _re.compile(r"[A-Za-z0-9_./\\-]+\.(?:py|js|ts|sh)\b")
_ORACLE_TEST_SURFACE = _re.compile(r"(?:^|/)test_[A-Za-z0-9_.\-]+\.(?:py|js|ts|sh)$")
_ORACLE_NEVER = _re.compile(r"(?!x)x")  # matches nothing: languages without a deterministic idiom

_ORACLE_SIGNALS = {
    ".py": {
        "assert": _re.compile(r"^\s*(assert\b|self\.assert\w*\(|raise AssertionError)"),
        "skip": _re.compile(r"pytest\.mark\.(skip|xfail)|pytest\.(skip|xfail)\(|self\.skipTest\(|unittest\.skip"),
        "patch": _re.compile(r"\bmonkeypatch\b|mock\.patch|unittest\.mock|\bMagicMock\b|\bpatch\("),
        "exit0": _re.compile(r"sys\.exit\(0\)|os\._exit\(0\)|SystemExit\(0\)"),
    },
    ".js": {
        "assert": _re.compile(r"^\s*(await\s+)?(expect\(|assert[.(]|console\.assert\()"),
        "skip": _re.compile(r"\b(it|test|describe)\.skip\(|\bx(it|test|describe)\("),
        "patch": _re.compile(r"\bjest\.mock\(|\bvi\.mock\(|\bsinon\.(stub|fake|mock)\b"),
        "exit0": _re.compile(r"process\.exit\(0\)"),
    },
    ".sh": {
        # a shell oracle's teeth are its failure paths: exit-1 branches, set -e, || fail/die.
        # No exit-0 signal: sh harnesses embed stub scripts (heredoc git/curl fakes) whose legit
        # 'exit 0' branches are line-locally indistinguishable from a short-circuit — proven a
        # false critical on the real board (56c435756089). Gutting the oracle still fires via
        # net-removed failure paths.
        "assert": _re.compile(r"\bexit 1\b|\|\|\s*(fail|die)\b|\bset -e\b"),
        "skip": _ORACLE_NEVER,
        "patch": _ORACLE_NEVER,
        "exit0": _ORACLE_NEVER,
    },
}
_ORACLE_SIGNALS[".ts"] = _ORACLE_SIGNALS[".js"]

# .py aliases: asserting_helpers and the Python helper-credit path read these directly.
_ORACLE_ASSERT = _ORACLE_SIGNALS[".py"]["assert"]
_ORACLE_SKIP = _ORACLE_SIGNALS[".py"]["skip"]
_ORACLE_PATCH = _ORACLE_SIGNALS[".py"]["patch"]
_ORACLE_EXIT0 = _ORACLE_SIGNALS[".py"]["exit0"]


def _oracle_vc_files(vc):
    """The files the frozen verification_command RUNS: its path-shaped .py/.js/.ts/.sh tokens
    OUTSIDE quoted spans, normalized. A path inside a quote is the command's string DATA, not a
    file it executes — an inline `python -c` ABSENCE oracle names a file precisely so it can
    assert the file is GONE, and counting that name as the receipt's oracle indicts the very
    deletion the receipt demanded (live: task 0013, the watchdog-guard retirement)."""
    vc = vc or ""
    spans = _string_spans(vc)
    out = set()
    for m in _ORACLE_VC_PATH.finditer(vc):
        if not any(a <= m.start() <= b for a, b in spans):
            out.add(m.group(0).replace("\\", "/").lstrip("./"))
    return out


# A verification_command RUNS code when it invokes a code interpreter / test runner, or names a
# source / script file it executes. This is language-agnostic on purpose: the ancestry audit is
# `git merge-base --is-ancestor`, which does not care whether the proof is Python, a shell script,
# or a JS runner — a done task that shipped code and proves it by running code must have that code
# on master. (The tamper audit is stricter and signal-table-bound by nature — it can only
# diff-analyze files whose language has a deterministic idiom in _ORACLE_SIGNALS — so it keys on
# _oracle_vc_files, NOT on this predicate.)
_CODE_VC_RUNNER = _re.compile(
    r"(?i)(?<![\w-])(python[0-9.]*|py|pytest|unittest|node|npm|npx|deno|bun|tsx|ts-node|"
    r"bash|sh|zsh|pwsh|powershell|go|cargo|ruby|rake|rspec|jest|mocha|vitest|phpunit|dotnet)"
    r"(?![\w-])")
_CODE_VC_EXT = _re.compile(
    r"(?i)\.(py|js|mjs|cjs|ts|tsx|jsx|vue|svelte|css|scss|sass|less|sh|bash|zsh|ps1|psm1|go|rs|"
    r"rb|java|kt|kts|scala|c|cc|cpp|cxx|h|hpp|php|swift)(?![\w])")


def code_shaped_vc(vc):
    """STRUCTURAL 'this task ships verifiable code' signal: the verification_command invokes a code
    runner (python/pytest/node/bash/go/...) or names a source/script file it runs. It keys audit
    eligibility on what the frozen PROOF does — not on work_kind, a free-form self-label a worker
    could set to 'governance'/'content' to slip a code task past the tamper and ancestry audits. The
    vc is still worker-authored, but it is constrained by the vc-validity gate and FROZEN at first
    active, so — unlike the label — it cannot be re-aimed after the fact."""
    vc = vc or ""
    return bool(_CODE_VC_RUNNER.search(vc) or _CODE_VC_EXT.search(vc))


_ORACLE_DEF = _re.compile(r"^\s*def \w+\s*\(")
_ORACLE_COMMENT = {".py": ("#",), ".sh": ("#",), ".js": ("//", "/*", "*"), ".ts": ("//", "/*", "*")}


def _string_spans(s):
    """Single-line quoted spans (start..end index, escapes honored) — CLOSED spans only. An
    unterminated opener yields no span: one line is the whole context a --unified=0 diff gives
    us, line-local analysis cannot prove the remainder inert, and this guard fails toward
    firing (an unterminated string must not launder a token — 9664 ruling)."""
    spans, i, n = [], 0, len(s)
    while i < n:
        if s[i] in "\"'":
            q, j = s[i], i + 1
            while j < n and s[j] != q:
                j += 2 if s[j] == "\\" else 1
            if j >= n:
                break
            spans.append((i, j))
            i = j + 1
        else:
            i += 1
    return spans


def _code_hit(body, rx, ext=".py"):
    """True iff rx matches in CODE position — outside every quoted span (a signature token that
    lives only inside a string literal is fixture DATA, and an inert string cannot disable any
    assertion; oracle-tamper-fixture-fp ruling) and not on a comment line (the guard's own
    self-tests carry these idioms in commentary; proven fleet-blocking false criticals
    a3d90b4fc214 / d024f87d5017 / 0f7ffc8aa20a)."""
    stripped = body.lstrip()
    for marker in _ORACLE_COMMENT.get(ext, ()):
        if stripped.startswith(marker):
            return False
    spans = None
    for m in rx.finditer(body):
        if spans is None:
            spans = _string_spans(body)
        if not any(a <= m.start() <= b for a, b in spans):
            return True
    return False


def _oracle_diff_path(raw):
    """Normalize one diff header path ('a/x', 'b/x', '/dev/null') to repo-relative or None."""
    p = raw.strip()
    if p == "/dev/null":
        return None
    if p[:2] in ("a/", "b/"):
        p = p[2:]
    return p.replace("\\", "/")


def _oracle_ext(path):
    i = path.rfind(".")
    return path[i:].lower() if i >= 0 else ""


def _oracle_pre_image(diff_text):
    """Every path that existed BEFORE the commit: '--- a/…' hunk headers plus pure-rename
    'rename from …' headers (a 100%-similarity rename emits no hunks at all)."""
    pre = set()
    for line in (diff_text or "").splitlines():
        if line.startswith("--- ") and not line.startswith("--- /dev/null"):
            p = _oracle_diff_path(line[4:])
            if p:
                pre.add(p)
        elif line.startswith("rename from "):
            pre.add(line[len("rename from "):].strip().replace("\\", "/"))
    return pre


def oracle_diff_counts(diff_text, vc_files, helpers=None, extra_pre_image=()):
    """The raw tamper measurements for ONE unified diff: {removed, added, flagged{skip,patch,
    exit0}}. Split out from analyze_oracle_diff so the adapter can NET the assertion counts
    across every commit a task recorded — see oracle_tamper_adapter for why that netting is
    what makes the guard's own remediation reachable.

    In scope: vc-named files (full signals) and commit-touched test_*.{py,js,ts,sh} files
    (assert-removal / skip / exit-0), matched on the file-pair's old OR new path — renaming an
    oracle away, or deleting it outright, keeps the pair in scope. `extra_pre_image` carries
    paths known to pre-exist from a task's OTHER commits, so a rename-away in one commit and a
    hollow recreate in the next is still an edit (the split-commit bypass).

    Two authorship exemptions bound the added-line signature checks (skip/patch/exit0):
      - a file BORN in the commit (new-file authorship is not tamper) — and a path present in
        the pooled pre-image is an existing oracle, whatever its '--- /dev/null' says; and
      - a WHOLLY-ADDITIVE hunk that introduces a new `def` (a brand-new test function cannot
        disable a pre-existing assertion; its skip/mock plumbing is authorship).
    A signature token in a string literal or on a comment line never counts (_code_hit).
    The mock/patch signal fires only on vc-named files: mock plumbing on the broader touched
    test surface is ordinary authorship, not oracle tamper.

    Assertion NET-REMOVAL is measured, not judged, here — and a REFACTOR is not a deletion:
    when a hunk replaces an inline assertion with a call to a helper that itself asserts, the
    assertion moved one frame down and the oracle is no less strict (often stricter — the live
    3d228169c184 case swapped an inline claim+assert for a helper that claims AND activates).
    `helpers` is the set of names in the post-image oracle files whose own bodies assert
    (asserting_helpers); a call to one credits at most one removal in that hunk. Absent that
    set, nothing is credited — the exemption can only ever be granted on evidence, never
    assumed."""
    helpers = helpers or set()
    pre_image = _oracle_pre_image(diff_text) | set(extra_pre_image)

    def _in_vc(path):
        return path is not None and any(path == f or path.endswith("/" + f) for f in vc_files)

    def _on_surface(path):
        return path is not None and bool(_ORACLE_TEST_SURFACE.search(path))

    current, vc_scope, in_scope, is_new, sig, ext, last_minus = None, False, False, False, None, "", ""
    recreated = False
    # Prefix columns in the diff body: 1 for an ordinary diff, one PER PARENT in a combined
    # (merge) diff. Set from each hunk header; 1 until the first one is seen.
    cols = 1
    removed_asserts = added_asserts = 0
    flagged = {"skip": set(), "patch": set(), "exit0": set()}
    hunk = []  # buffered (sign, body) for the open hunk of the in-scope file

    def _flush():
        nonlocal removed_asserts, added_asserts
        if not hunk:
            return
        if sig is None:
            hunk.clear()
            return
        # A rename-recreated file arrives as one wholly-additive hunk full of defs — that is the
        # file's own body wearing a fresh inode, not a new test function added to an existing
        # oracle, so the additive-def authorship exemption must not swallow it.
        wholly_additive_def = (not recreated
                               and not any(s == "-" for s, _ in hunk)
                               and any(_ORACLE_DEF.match(b) for s, b in hunk if s == "+"))
        # An assertion swapped for a call to an asserting helper MOVED; it was not deleted. Counted
        # per hunk and capped by that hunk's own removals, so one helper call can never excuse a
        # wholesale gutting of assertions elsewhere in the commit.
        # ASSERTION COUNTS ARE VC-CONFINED: the invariant is about the oracle that granted the
        # receipt, and only the frozen verification_command names that oracle. A test file
        # deleted elsewhere in the battery is a ruled retirement (watchdog guard, a replaced
        # console suite, retired lanes) whose absence is the guard CENSUS's beat — counting its
        # asserts here made every ruled retirement an unclearable critical (proven live on
        # tasks 0013/0021/rule-the-unruled-drops the day the surface scope landed). The surface
        # expansion exists for the SILENCING signals below: skip/exit-0 on a battery member the
        # vc never names is still tamper on the deploy gate.
        hunk_removed = sum(1 for s, b in hunk if s == "-" and sig["assert"].search(b)) if vc_scope else 0
        moved = 0
        if hunk_removed and helpers:
            called = {n for s, b in hunk if s == "+" for n in _ORACLE_CALL.findall(b)}
            moved = min(hunk_removed, len(called & helpers))
        added_asserts += moved
        for sign, body in hunk:
            if vc_scope and sign == "-" and sig["assert"].search(body):
                removed_asserts += 1
            elif sign == "+":
                if vc_scope and sig["assert"].search(body):
                    added_asserts += 1
                if not is_new and not wholly_additive_def:
                    if _code_hit(body, sig["skip"], ext):
                        flagged["skip"].add(current)
                    if vc_scope and _code_hit(body, sig["patch"], ext):
                        flagged["patch"].add(current)
                    if _code_hit(body, sig["exit0"], ext):
                        flagged["exit0"].add(current)
        hunk.clear()

    for line in (diff_text or "").splitlines():
        if line.startswith("--- "):
            _flush()
            last_minus = line[4:].strip()
            continue
        if line.startswith("+++ "):
            old = _oracle_diff_path(last_minus)
            new = _oracle_diff_path(line[4:])
            current = new or old
            # old-or-new scoping: renaming an oracle away, or deleting it outright, keeps the
            # pair in scope — the minus lines still count against the oracle they gutted.
            vc_scope = _in_vc(old) or _in_vc(new)
            in_scope = vc_scope or _on_surface(old) or _on_surface(new)
            ext = _oracle_ext(current) if current else ""
            sig = _ORACLE_SIGNALS.get(ext)
            in_scope = in_scope and sig is not None
            # A file BORN in this commit cannot weaken a pre-existing oracle: skip/patch/exit
            # plumbing in a brand-new test (this repo's own `raise SystemExit(main())` and
            # embedded-fixture idioms) is authorship, not tamper. Edits to EXISTING oracle
            # files get the full treatment — and a path present in the commit's pre-image
            # (rename-recreate) is an existing oracle, whatever the '--- /dev/null' says.
            is_new = (last_minus == "/dev/null") and (current not in pre_image)
            recreated = (last_minus == "/dev/null") and (current in pre_image)
            continue
        if line.startswith("@@"):
            _flush()
            # COMBINED (merge) DIFFS carry one prefix column PER PARENT: `git show` on a merge
            # emits `@@@ ... @@@` and lines like `++    assert ...`. Read the width from the
            # header's run of '@' rather than assuming one column. Assuming one is how a merge
            # that REPLACED an assertion read as a net removal - the `-` line stripped to
            # `assert ...` and counted, while the `++` line stripped to `+    assert ...` and did
            # not - a false CRITICAL on every conflict-resolving merge, and criticals block every
            # completion in the fleet.
            cols = max(1, len(line) - len(line.lstrip("@")) - 1)
            continue
        if not in_scope or len(line) <= cols or line.startswith(("+++", "---")):
            continue
        prefix, body = line[:cols], line[cols:]
        if "+" in prefix:
            hunk.append(("+", body))
        elif "-" in prefix:
            hunk.append(("-", body))
    _flush()
    return {"removed": removed_asserts, "added": added_asserts, "flagged": flagged}


def oracle_reasons(net_removed, flagged):
    """Reasons from measurements: a positive NET removal plus any added-signature flags."""
    reasons = []
    if net_removed > 0:
        reasons.append("net-removed %d assertion(s) in oracle files" % net_removed)
    if flagged["skip"]:
        reasons.append("added skip/xfail in %s" % ", ".join(sorted(flagged["skip"])))
    if flagged["patch"]:
        reasons.append("added mock/monkeypatch of the oracle in %s" % ", ".join(sorted(flagged["patch"])))
    if flagged["exit0"]:
        reasons.append("added exit-0 short-circuit in %s" % ", ".join(sorted(flagged["exit0"])))
    return reasons


def analyze_oracle_diff(diff_text, vc_files, helpers=None, extra_pre_image=()):
    """One-diff view of the same measurements (tests and single-commit callers): assertion
    counts net WITHIN this diff only. The adapter nets across a task's whole commit set via
    oracle_diff_counts — see its docstring for scope, exemptions, and the helper credit."""
    c = oracle_diff_counts(diff_text, vc_files, helpers=helpers, extra_pre_image=extra_pre_image)
    return oracle_reasons(c["removed"] - c["added"], c["flagged"])

_ORACLE_DIFF_MEMO = {}   # (repo_dir, sha) -> unified diff text; commit content is immutable

# A commit's tamper verdict is immutable for a fixed analyzer, so it is cached ACROSS PROCESSES.
# The in-process memo above only ever helped a second call inside one run; every audit still paid a
# full cold walk of every done task's commits (measured 55.9s cold against 0.2s warm on the live
# board, which is why the per-adapter deadline had to be raised to 180s). The cost is structural: it
# is O(done code tasks x their commits) git spawns, so it grows with the ledger and no bound survives.
#
# THE CACHE MUST NOT BE ABLE TO HIDE A TAMPER. Every key carries a fingerprint of the analysis code
# itself, so strengthening the detector invalidates every verdict it predates rather than reading back
# a clean answer computed by the weaker one. That is the difference between memoising a pure function
# and remembering a conclusion.
_ORACLE_FINGERPRINT = None


def _oracle_fingerprint():
    """Hash of every function and pattern that decides a tamper verdict. Anything that could change
    an answer must be in here, or a cached clean verdict outlives the analyzer that earned it."""
    global _ORACLE_FINGERPRINT
    if _ORACLE_FINGERPRINT is None:
        import hashlib
        import inspect
        # EVERY function that can change an answer — not just the entry point. analyze_oracle_diff
        # is now a thin wrapper over oracle_diff_counts/oracle_reasons, so fingerprinting the
        # wrapper alone let the whole decision body change without invalidating a cached verdict:
        # a tamper audit switched off by having already run. Same class as a guard holding a copy
        # of the code's filename — the list of what decides must not be a stale second copy.
        parts = []
        for obj in (analyze_oracle_diff, oracle_diff_counts, oracle_reasons, asserting_helpers,
                    _oracle_vc_files, _code_hit, _oracle_pre_image, _string_spans):
            try:
                parts.append(inspect.getsource(obj))
            except (OSError, TypeError):
                parts.append(repr(obj))          # fingerprint degrades, never silently succeeds
        parts += [repr(_ORACLE_SIGNALS), repr(_ORACLE_TEST_SURFACE), repr(_ORACLE_VC_PATH)]
        _ORACLE_FINGERPRINT = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:16]
    return _ORACLE_FINGERPRINT


_GIT_DIR_MEMO = {}   # str(repo_dir) -> absolute .git dir (or None); a repo's .git never moves
# The persistent cache file names, NAMED HERE so a test that must run cold deletes the file this
# code actually writes. A second copy of a filename in a test is how a "cold" run silently became
# a warm one and blinded every seeded regression that depended on it.
ORACLE_VERDICT_CACHE_NAME = "hub-oracle-verdicts.sqlite3"
COMMIT_TREE_CACHE_NAME = "hub-commit-trees.sqlite3"


class _OracleVerdictCache:
    """Persistent (sha, oracle-files, analyzer) -> verdict store, one per repo.

    Lives in the repo's own .git so it is per-repository for free (a temp repo in a test gets a fresh
    one) and never enters a commit. Every failure mode degrades to NOT CACHING: a read-only .git, a
    locked db or a corrupt file must slow the audit down, never weaken it or crash it."""

    def __init__(self, repo_dir):
        self._db = None
        self._pending = []
        try:
            import sqlite3
            import subprocess
            # Resolving .git shells git ONCE per repo per process: this constructor runs on every
            # adapter pass, and an unmemoized rev-parse here broke the memoized-second-pass
            # zero-spawn property the fast-lanes test pins.
            rkey = str(repo_dir)
            if rkey not in _GIT_DIR_MEMO:
                r = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "--absolute-git-dir"],
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=20)
                _GIT_DIR_MEMO[rkey] = r.stdout.strip() if r.returncode == 0 else None
            if not _GIT_DIR_MEMO[rkey]:
                return
            path = _os.path.join(_GIT_DIR_MEMO[rkey], ORACLE_VERDICT_CACHE_NAME)
            db = sqlite3.connect(path, timeout=10)
            db.execute("CREATE TABLE IF NOT EXISTS verdicts "
                       "(k TEXT PRIMARY KEY, reasons TEXT NOT NULL)")
            db.commit()
            self._db = db
        except Exception:
            self._db = None

    @staticmethod
    def _key(sha, vc_files, extra=""):
        # `extra` carries the task's pooled pre-image digest: the same sha analyzed under a
        # different task pre-image is a DIFFERENT verdict (rename-recreate / split-commit), so
        # it must never be a stale hit. The analyzer fingerprint already invalidates on any
        # analyzer change — counts-shaped rows never collide with legacy reasons-shaped ones.
        return "%s|%s|%s|%s" % (_oracle_fingerprint(), sha, ",".join(sorted(vc_files)), extra)

    def get(self, sha, vc_files, extra=""):
        """The cached reasons list, or None for a miss. A miss and an empty verdict are DIFFERENT:
        `[]` means analysed and clean, and conflating them would re-walk every clean commit forever."""
        if self._db is None:
            return None
        try:
            row = self._db.execute("SELECT reasons FROM verdicts WHERE k=?",
                                   (self._key(sha, vc_files, extra),)).fetchone()
            return _json.loads(row[0]) if row else None
        except Exception:
            return None

    def put(self, sha, vc_files, reasons, extra=""):
        """Queued, not committed: a commit() per verdict is an fsync per commit, which on a cold walk
        of the whole ledger cost more than the git call it exists to save. close() flushes."""
        self._pending.append((self._key(sha, vc_files, extra), _json.dumps(reasons)))

    def flush(self):
        if self._db is None or not self._pending:
            self._pending = []
            return
        try:
            self._db.executemany("INSERT OR REPLACE INTO verdicts(k,reasons) VALUES(?,?)",
                                 self._pending)
            self._db.commit()
        except Exception:
            pass
        self._pending = []

    def close(self):
        self.flush()
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None


# The delimiter that splits one `git show` over many revisions back into per-commit diffs. It goes
# through --format, so it travels as a COMMAND-LINE ARGUMENT: it may not contain a NUL. The obvious
# choice did, and Windows CreateProcess rejects an argument with an embedded null - subprocess raised
# ValueError, _batch_diffs swallowed it, and the adapter fell back to one `git show` per commit. The
# batching that the whole cold-cost fix rests on had therefore never once run on this platform, and
# nothing said so: a silent fallback to the correct-but-slow path is invisible from the outside.
# Freshly random per process, so no diff body can contain it (a literal marker is text a commit could
# legitimately carry, and then a diff would split itself in half).
_DIFF_SPLIT = "\x1eEMBER-COMMIT-" + _uuid.uuid4().hex + "\x1e"

# Batched vs per-commit git fetches, so a fallback is COUNTABLE rather than merely slow. The guard
# holds the adapter to ~zero marginal spawns per commit; a wall-clock bound alone cannot tell a
# regression from a loaded machine, and this one is deterministic under any load.
_BATCH_STATS = {"batched": 0, "fallback": 0, "batch_failures": [],
                "tree_batched": 0, "tree_fallback": 0}


class _CommitTreeCache:
    """Persistent (sha -> tree) store, one per repo. A commit's tree is as immutable as its diff.

    Same shape and same failure doctrine as _OracleVerdictCache: it lives in the repo's own .git so
    a temp repo in a test gets a fresh one and it never enters a commit, and every failure mode
    degrades to NOT CACHING — a read-only .git, a locked db or a corrupt file must slow a run down,
    never weaken it or crash it.

    KEYED ON THE SHA THE BOARD STORES, verbatim. The commit seam records abbreviated shas
    (`fc48ca087dc8`) as readily as full ones, and keying on git's resolved answer instead is what
    made the oracle batch miss on every abbreviated lookup while its guard read green."""

    def __init__(self, repo_dir):
        self._db = None
        self._pending = []
        try:
            import sqlite3
            import subprocess
            # Same memo as the verdict cache: this constructor runs on every audit pass, and an
            # unmemoized rev-parse here is a git spawn on every WARM audit — the exact cost the
            # hot-memoize guard bounds at zero.
            rkey = str(repo_dir)
            if rkey not in _GIT_DIR_MEMO:
                r = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "--absolute-git-dir"],
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=20)
                _GIT_DIR_MEMO[rkey] = r.stdout.strip() if r.returncode == 0 else None
            if not _GIT_DIR_MEMO[rkey]:
                return
            path = _os.path.join(_GIT_DIR_MEMO[rkey], COMMIT_TREE_CACHE_NAME)
            db = sqlite3.connect(path, timeout=10)
            db.execute("CREATE TABLE IF NOT EXISTS trees (sha TEXT PRIMARY KEY, tree TEXT NOT NULL)")
            db.commit()
            self._db = db
        except Exception:
            self._db = None

    def get_many(self, shas):
        """{sha: tree} for the shas already known. A miss is simply absent — there is no "analysed
        and empty" verdict here, because a commit either has a tree or is not a commit."""
        if self._db is None or not shas:
            return {}
        out = {}
        try:
            todo = list(shas)
            while todo:
                chunk, todo = todo[:400], todo[400:]
                q = "SELECT sha, tree FROM trees WHERE sha IN (%s)" % ",".join("?" * len(chunk))
                for row in self._db.execute(q, chunk):
                    out[row[0]] = row[1]
        except Exception:
            return out
        return out

    def put(self, sha, tree):
        """Queued, not committed: a commit() per row is an fsync per row, which on a cold walk of
        the whole ledger costs more than the git call it exists to save. flush() writes them."""
        self._pending.append((sha, tree))

    def flush(self):
        if self._db is None or not self._pending:
            self._pending = []
            return
        try:
            self._db.executemany("INSERT OR REPLACE INTO trees(sha,tree) VALUES(?,?)", self._pending)
            self._db.commit()
        except Exception:
            pass
        self._pending = []

    def close(self):
        self.flush()
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None


def resolve_commit_trees(shas, repo_dir, cache=None):
    """{sha: tree} for every resolvable sha, in ONE git spawn for the whole cold set.

    `git cat-file --batch-check` is line-oriented over stdin, so unlike the diff batch there is no
    delimiter to smuggle through --format and no NUL to have Windows CreateProcess reject: the
    trap that silently disabled the oracle batch for its whole life cannot arise here.

    Results are correlated BY INPUT ORDER, never by parsing a sha back out of the output. git
    answers a resolvable request with the resolved OID — which for an abbreviated input is a
    different string than we asked about — and echoes the input only when it is missing. Matching
    on the returned oid would therefore drop exactly the abbreviated shas the board actually
    stores, which is the other half of the same defect.

    Degrades to per-sha spawns if the batch cannot run at all, and says so in _BATCH_STATS so the
    fallback is countable rather than merely slow."""
    import subprocess
    shas = [s for s in dict.fromkeys(str(x or "").strip() for x in shas) if s]
    known = cache.get_many(shas) if cache is not None else {}
    missing = [s for s in shas if s not in known]
    if not missing:
        return dict(known)
    out = dict(known)
    try:
        r = subprocess.run(["git", "-C", str(repo_dir), "cat-file", "--batch-check"],
                           input="".join(f"{s}^{{tree}}\n" for s in missing),
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=120)
        lines = r.stdout.splitlines() if r.returncode == 0 else []
        if len(lines) != len(missing):
            raise ValueError(f"batch-check returned {len(lines)} lines for {len(missing)} requests")
        _BATCH_STATS["tree_batched"] += 1
        for sha, line in zip(missing, lines):
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "tree":
                out[sha] = parts[0]
                if cache is not None:
                    cache.put(sha, parts[0])
        return out
    except Exception as e:
        _note_batch_failure(f"cat-file --batch-check: {e}")
    for sha in missing:                      # last resort: correct, and COUNTED as slow
        _BATCH_STATS["tree_fallback"] += 1
        try:
            r = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", sha + "^{tree}"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                out[sha] = r.stdout.strip()
                if cache is not None:
                    cache.put(sha, r.stdout.strip())
        except OSError:
            pass
    return out


def _note_batch_failure(msg):
    """Bounded: a repo where every batch fails must not grow this list without limit — the counter
    beside it already carries the magnitude, and the first few messages carry the reason."""
    if len(_BATCH_STATS["batch_failures"]) < 20:
        _BATCH_STATS["batch_failures"].append(msg)


def _batch_diffs(repo_dir, shas):
    """Every commit's diff in ONE `git show`, not one spawn per commit.

    This is where the cold cost lived: a spawn costs ~250ms on this platform, and the adapter paid two
    per commit (the diff, then the oracle file's post-image), so cost scaled with the ledger at a
    quarter-second a step. `git show` accepts many revisions and renders them in order, so a marker in
    --format splits them back apart. Merges must be excluded by the caller: git renders those as a
    COMBINED diff whose two-column prefixes read as removals.

    A failure degrades to the caller's per-commit path — correctness never depends on the batch — but
    it is RECORDED, because the failure this function actually had was one nobody could see."""
    import subprocess
    out, found = {}, 0
    ordered = [s for s in dict.fromkeys(shas) if s]
    for chunk in (ordered[i:i + 200] for i in range(0, len(ordered), 200)):
        try:
            r = subprocess.run(["git", "-C", str(repo_dir), "show", "--format=" + _DIFF_SPLIT + "%H",
                                "--unified=0"] + list(chunk),
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=300)
        except Exception as exc:
            _note_batch_failure("%s: %s" % (type(exc).__name__, exc))
            continue
        if r.returncode != 0:
            _note_batch_failure("git show rc=%s: %s" % (r.returncode, (r.stderr or "")[:200]))
            continue
        # %H answers in the FULL sha, but the board records abbreviations (a commit record carries
        # `fc48ca087dc8`). Keying only on what git echoed put every diff under a name the caller never
        # asks for, so the batch filled and every lookup still missed: on the live board that was 114
        # batched diffs and 114 per-commit `git show` spawns anyway. Store the caller's own spelling
        # too, resolved by exact prefix - git's own identity rule for an abbreviated sha, not a
        # similarity test.
        by_len = {}
        for s in chunk:
            by_len.setdefault(len(s), set()).add(s)
        for part in r.stdout.split(_DIFF_SPLIT):
            if not part.strip():
                continue
            head, _, body = part.partition("\n")
            sha = head.strip()
            if not sha:
                continue
            out[sha] = body
            found += 1                       # COMMITS covered, not dict keys: the alias below adds a
            for n, group in by_len.items():  # second key for the same diff and would double the count
                if sha[:n] in group:
                    out[sha[:n]] = body
    _BATCH_STATS["batched"] += found
    return out


def _batch_blobs(repo_dir, refs):
    """`<sha>:<path>` -> file text for many refs in ONE `git cat-file --batch`. A missing path is
    simply absent (a commit that predates an oracle file is not a finding here)."""
    import subprocess
    out = {}
    ordered = [r for r in dict.fromkeys(refs) if r]
    if not ordered:
        return out
    try:
        p = subprocess.run(["git", "-C", str(repo_dir), "cat-file", "--batch"],
                           input=("\n".join(ordered) + "\n").encode("utf-8"),
                           capture_output=True, timeout=300)
    except Exception:
        return out
    if p.returncode != 0:
        return out
    buf, i, idx = p.stdout, 0, 0
    while i < len(buf) and idx < len(ordered):
        nl = buf.find(b"\n", i)
        if nl < 0:
            break
        header = buf[i:nl].decode("utf-8", "replace").split()
        i = nl + 1
        if len(header) >= 3 and header[1] == "blob":
            size = int(header[2])
            out[ordered[idx]] = buf[i:i + size].decode("utf-8", "replace")
            i += size + 1                       # the trailing newline cat-file appends
        # a `missing`/`ambiguous` line consumes no body
        idx += 1
    return out


def _merge_shas(repo_dir, shas):
    """Which of these shas are merges, in ONE git call rather than one per commit.
    An unresolvable sha is simply absent from the answer — existence is another guard's beat."""
    import subprocess
    merges, ordered = set(), sorted(set(shas))
    if not ordered:
        return merges
    for chunk in (ordered[i:i + 400] for i in range(0, len(ordered), 400)):
        try:
            r = subprocess.run(["git", "-C", str(repo_dir), "rev-list", "--parents", "--no-walk"]
                               + list(chunk),
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=60)
        except Exception:
            continue
        if r.returncode != 0:
            continue
        for line in r.stdout.splitlines():
            f = line.split()
            if len(f) > 2:
                merges.add(f[0])
                # rev-list answers in full sha; the caller may hold an abbreviation.
                for s in chunk:
                    if f[0].startswith(s):
                        merges.add(s)
    return merges


def oracle_tamper_adapter(state, repo_dir=".", git_diff=None, cache_dir=None):
    """For each done product/verification task, audit the diff of every commit that names it.
    git_diff(sha) -> unified diff text or None (injectable for tests); the default shells git.
    An unresolvable sha is skipped — commit existence is the evidence/commit guards' beat."""
    import subprocess
    from pathlib import Path
    import hashlib
    import json
    import os

    # RESET PER RUN. _BATCH_STATS is module-global, so without this the failure list is a
    # cumulative smear across every audit in the process and its count means nothing.
    _BATCH_STATS["batch_failures"] = []

    cache_root = Path(cache_dir) if cache_dir else None

    def _cache_path(kind, key, suffix):
        if cache_root is None:
            return None
        digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
        return cache_root / kind / (digest + suffix)

    def _cache_write(path, text):
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    merge_set = {}          # filled once, below, from ONE rev-list over every sha in play

    def _default_diff(sha):
        # A commit's diff is IMMUTABLE — memoize per (repo, sha) so repeated audits over the
        # same dones stop re-spawning git (snapshot-audit-fast-lanes). Only successful lookups
        # cache: a sha that resolves later (branch landed) must not be pinned to None.
        key = (str(repo_dir), str(sha))
        if key in _ORACLE_DIFF_MEMO:
            return _ORACLE_DIFF_MEMO[key]
        disk = _cache_path("diff", key, ".patch")
        if disk is not None:
            try:
                out = disk.read_text(encoding="utf-8")
                _ORACLE_DIFF_MEMO[key] = out
                return out
            except OSError:
                pass
        try:
            # A MERGE commit is skipped, and this is not leniency. `git show` renders a merge as a
            # COMBINED diff whose line prefixes are two characters (`++`, `--`), so a 1:1 assertion
            # replacement inside a conflict resolution reads to a prefix counter as a net REMOVAL -
            # observed 2026-08-05, where a merge that swapped one assert for a stronger one raised
            # oracle:tamper CRITICAL and, because audit_unsound refuses every completion while a
            # critical stands, wedged the whole fleet's ability to finish anything. A merge also
            # authors nothing: every change it integrates arrived in a non-merge commit that names
            # its own task and was analysed there. So there is no coverage to lose by skipping it,
            # and a real weakening still cannot hide behind one.
            # Answered from the ONE batched rev-list rather than a spawn per commit: on a board with
            # hundreds of done code commits, that per-sha spawn was half the cold cost by itself.
            if sha in merge_set.get("shas", ()):
                _ORACLE_DIFF_MEMO[key] = ""
                return ""
            # UTF-8 with a defined error policy: text=True on Windows decodes cp1252 and DIES
            # on a diff containing e.g. curly quotes — the reader thread's UnicodeDecodeError
            # silently skipped that commit's tamper analysis (observed live 2026-08-03).
            _BATCH_STATS["fallback"] += 1
            r = subprocess.run(["git", "-C", str(repo_dir), "show", "--format=", "--unified=0", sha],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=20)
            out = r.stdout if r.returncode == 0 else None
        except Exception:
            return None
        if out is not None:
            if len(_ORACLE_DIFF_MEMO) > 1024:
                _ORACLE_DIFF_MEMO.clear()
            _ORACLE_DIFF_MEMO[key] = out
            _cache_write(disk, out)
        return out

    def _helpers_at(sha, vc_files):
        # The post-image of the oracle files this commit's receipt was proven by: which of their
        # functions assert. Same memo discipline as the diff — a commit's tree is immutable.
        key = (str(repo_dir), str(sha), "helpers", tuple(sorted(vc_files)))
        if key in _ORACLE_DIFF_MEMO:
            return _ORACLE_DIFF_MEMO[key]
        disk = _cache_path("helpers", key, ".json")
        if disk is not None:
            try:
                names = set(json.loads(disk.read_text(encoding="utf-8")))
                _ORACLE_DIFF_MEMO[key] = names
                return names
            except (OSError, ValueError):
                pass
        names = set()
        for f in sorted(vc_files):
            try:
                r = subprocess.run(["git", "-C", str(repo_dir), "show", f"{sha}:{f}"],
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=20)
            except Exception:
                continue
            if r.returncode == 0:
                names |= asserting_helpers(r.stdout)
        if len(_ORACLE_DIFF_MEMO) > 1024:
            _ORACLE_DIFF_MEMO.clear()
        _ORACLE_DIFF_MEMO[key] = names
        _cache_write(disk, json.dumps(sorted(names)))
        return names

    injected = git_diff is not None          # a test's stub diff must never touch a real repo's cache
    git_diff = git_diff or _default_diff
    commits_by_task = {}
    for cm in state.get("by_type", {}).get("commit", []):
        if cm.get("task") and cm.get("sha"):
            commits_by_task.setdefault(cm["task"], []).append(cm["sha"])
        for task_id in (cm.get("also_tasks") or []):
            if task_id and cm.get("sha"):
                commits_by_task.setdefault(task_id, []).append(cm["sha"])
    cache = None if injected else _OracleVerdictCache(repo_dir)

    # STRUCTURAL eligibility, resolved first so the git work can be batched: a done task is
    # oracle-bearing when its frozen verification_command runs code (names .py oracle files) AND it
    # recorded code commits — never because it self-declared work_kind product/verification. Keying on
    # the label let a worker relabel a code task 'governance' to escape the tamper audit entirely.
    work = []
    for t in state.get("by_type", {}).get("task", []):
        if t.get("status") != "done":
            continue
        vc_files = _oracle_vc_files(t.get("verification_command"))
        if not vc_files:
            continue
        for sha in commits_by_task.get(t["id"], []):
            work.append((t, sha, vc_files))

    # ONLY CACHE MISSES COST ANYTHING — and a repeat audit spawns no git at all. Diff text is
    # memoized in-process and mirrored to the per-repo disk cache, so the pooled pre-image (which
    # needs every diff of a task) reads cached bytes on a warm pass. Batch-fetch whatever is not
    # yet memoized: ONE rev-list decides merges, ONE batched show fetches diffs, ONE batched
    # cat-file fetches oracle post-images.
    if work and not injected:
        unseen = sorted({sha for _, sha, _ in work if (str(repo_dir), str(sha)) not in _ORACLE_DIFF_MEMO})
        if unseen:
            merge_set["shas"] = _merge_shas(repo_dir, unseen)
            fresh = [s for s in unseen if s not in merge_set["shas"]]
            for sha, diff in _batch_diffs(repo_dir, fresh).items():
                _ORACLE_DIFF_MEMO[(str(repo_dir), sha)] = diff
            blob_texts = _batch_blobs(repo_dir, sorted({f"{sha}:{f}" for _, sha, vf in work for f in vf
                                                   if (str(repo_dir), str(sha), "helpers",
                                                       tuple(sorted(vf))) not in _ORACLE_DIFF_MEMO}))
            for _, sha, vf in work:
                # The SAME key shape _helpers_at reads (vc_files included): a prefetch stored under
                # a shorter key is a guaranteed miss that re-spawns git for every commit.
                key = (str(repo_dir), str(sha), "helpers", tuple(sorted(vf)))
                if key not in _ORACLE_DIFF_MEMO:
                    names = set()
                    for f in sorted(vf):
                        text = blob_texts.get(f"{sha}:{f}")
                        if text is not None:
                            names |= asserting_helpers(text)
                    _ORACLE_DIFF_MEMO[key] = names

    viols = []
    by_task = {}
    for t, sha, vc_files in work:
        by_task.setdefault(t["id"], (t, vc_files, []))[2].append(sha)
    for tid, (t, vc_files, shas) in by_task.items():
        # A merge commit's diff is the empty string (skipped, see _default_diff) and an
        # unresolvable sha is None — neither carries analyzable authorship.
        diffs = [(sha, git_diff(sha)) for sha in shas]
        diffs = [(sha, d) for sha, d in diffs if d]
        # pre-image pooled across ALL the task's commits: renaming the oracle away in one
        # commit and recreating it hollow in the next is one edit split in two, not authorship.
        # The pooled set rides the verdict-cache key (pre_digest): the same sha analyzed under a
        # different task pre-image is a DIFFERENT verdict, never a stale hit.
        task_pre_image = set()
        for _, d in diffs:
            task_pre_image |= _oracle_pre_image(d)
        pre_digest = hashlib.sha256("\n".join(sorted(task_pre_image)).encode("utf-8")).hexdigest()[:16]
        # ASSERTION COUNTS NET ACROSS THE TASK'S COMMITS; signature flags stay per-commit.
        # The finding is "this task's oracle is WEAKER than the one that was frozen", and that is
        # a property of the task's whole recorded diff, not of one commit in it. Scored per commit,
        # the guard's own remediation - "restore the weakened tests" - was unreachable: a commit's
        # diff is immutable, so a deleted assert stayed critical (and wedged every completion on
        # the board) no matter what was restored afterwards. Netting costs the guard nothing: a
        # tamperer who could pad the count in a follow-up commit could equally pad it in the same
        # commit. Real tamper - asserts deleted and never restored - still fires, forever.
        net_removed = 0
        for sha, diff in diffs:
            counts = cache.get(sha, vc_files, extra=pre_digest) if cache is not None else None
            if not isinstance(counts, dict) or "removed" not in counts:
                c = oracle_diff_counts(diff, vc_files, helpers=_helpers_at(sha, vc_files),
                                       extra_pre_image=task_pre_image)
                counts = {"removed": c["removed"], "added": c["added"],
                          "flagged": {k: sorted(v) for k, v in c["flagged"].items()}}
                if cache is not None:
                    cache.put(sha, vc_files, counts, extra=pre_digest)
            net_removed += counts["removed"] - counts["added"]
            reasons = oracle_reasons(0, counts["flagged"])
            if reasons:
                viols.append(_v(
                    "oracle:tamper:%s:%s" % (t["id"].rsplit(":", 1)[-1], str(sha)[:12]), "critical",
                    "the commit behind a receipt never weakens the oracle that granted it",
                    "commit %s on %s: %s" % (str(sha)[:12], t["id"], "; ".join(reasons)),
                    "oracle intact", kind="logic",
                    remediation="restore the weakened tests (or supersede the task with a ruled decision); "
                                "the receipt proved a weaker oracle than the one that was frozen"))
        if diffs and net_removed > 0:
            viols.append(_v(
                "oracle:tamper:%s:asserts" % t["id"].rsplit(":", 1)[-1], "critical",
                "the commits behind a receipt never leave the oracle with fewer assertions",
                "%s: net-removed %d assertion(s) across %d recorded commit(s) in oracle files"
                % (t["id"], net_removed, len(diffs)),
                "oracle intact", kind="logic",
                remediation="restore the removed assertions in %s (a later commit on this task "
                            "counts), or supersede the task with a ruled decision"
                            % ", ".join(sorted(vc_files))))
    if cache is not None:
        cache.close()
    # A GIT LOOKUP THAT FAILED IS A COMMIT THIS GUARD DID NOT ACTUALLY ANALYSE. _note_batch_failure
    # recorded those into a list that NOTHING read — a swallow with a filing cabinet, and the most
    # dangerous shape a guard can take: it reports clean while its coverage silently shrank. Warn,
    # not blocking: degraded coverage is a reason to look, not a reason to refuse a completion.
    failures = _BATCH_STATS.get("batch_failures") or []
    if failures:
        viols.append(_v(
            "oracle:degraded", "warn",
            "every commit behind a receipt is actually analysed (a git lookup that failed is a "
            "commit this guard skipped, not a commit it cleared)",
            "%d git lookup(s) failed during tamper analysis; first: %s"
            % (len(failures), "; ".join(str(f)[:120] for f in failures[:3])),
            "zero failed lookups", kind="probe",
            remediation="re-run the audit against a reachable repo; a persistent failure means the "
                        "tamper guard is running blind over those commits and its silence is not "
                        "evidence they are clean"))
    return viols


# ---- worker spin: a thrashing (worker, task), not just a dead one (live-worker-spin-guard) ----
# A dead worker stops emitting; a THRASHING one keeps emitting the SAME non-advancing events — k
# consecutive failed-finishes, or an A-B-A-B oscillation (claim->fail->claim->fail, active<->todo)
# — burning tokens on a loop that never moves the task's state frontier. This generalizes the
# poison consecutive-fail fold to (worker, task) and adds oscillation detection. AMBER (warn): a
# stuck worker is an operator signal to look, not proof the board is unsound — it never blocks a
# deploy. Source: pydantic-ai StuckLoopDetection (hash (worker, task, event-signature); flag k
# identical or A-B-A-B). Folds EVENTS, never state: last-write-wins state cannot see a repeated
# failed-finish, so a state-fed spin guard would be silently vacuous.

_SPIN_K_DEFAULT = 4


def _spin_event_sig(ev):
    """(worker, task, signature) for one event, or None if it carries no (worker,task) signal. The
    signature captures what the event DID so a repeat is detectable: a verification.recorded is
    ('verify','ok'|'fail'); a task.* event is ('status', <status>). Worker is the top-level
    agent_id, else the receipt's ran_by / payload.agent; task is the aggregate for task.* events,
    else payload.task."""
    typ = ev.get("type", "") or ""
    pl = ev.get("payload") or {}
    worker = ev.get("agent_id") or pl.get("ran_by") or pl.get("agent")
    if typ == "verification.recorded":
        task = pl.get("task")
        sig = ("verify", "ok" if pl.get("exit_code") == 0 else "fail")
    elif typ.startswith("task.") and pl.get("status"):
        # only a status the event actually SET is a trajectory point; a field-only edit (evidence,
        # deps, note, priority) carries NO 'status' key and is not a spin signal, and a re-save of
        # the same status is deduped in the fold — a worker enriching a task it is working is not
        # thrashing (adversarial-verify false-positive finding).
        task = ev.get("aggregate")
        sig = ("status", pl["status"])
    else:
        return None
    if not worker or not task:
        return None
    return worker, task, sig


def _spin_is_advance(sig):
    """A signature that RESETS the spin run — real forward progress. An exit-0 verification or a
    transition to 'done' is progress; a failed verification or any status revisit is not. (A bare
    version/result_version bump is deliberately NOT progress: it increments on active<->todo churn,
    so it cannot distinguish a thrash from a step.)"""
    return sig == ("verify", "ok") or sig == ("status", "done")


# A "regression/failure" signature: a failed finish, or a release back to 'todo' (the task went
# UN-claimed — a regression from active). A spin MUST involve one of these — an oscillation between
# two forward working states (active<->in_progress, active<->blocked-while-waiting) is not a stuck
# loop and must not amber-spam the operator (adversarial-verify false-positive finding).
_SPIN_REGRESS = frozenset({("verify", "fail"), ("status", "todo")})


def _spin_identical(run, k):
    """True if the run contains k CONSECUTIVE identical regress signatures (a stuck fail burst),
    scanned across the WHOLE run — so a trailing heartbeat/status touch after k fails cannot mask it
    (the false-negative the last-k-window approach had). After the fold's status-dedup, the only
    signature that can repeat consecutively is a failed finish, so this is k consecutive fails."""
    cur, prev = 0, None
    for sig in run:
        cur = cur + 1 if sig == prev else 1
        prev = sig
        if cur >= k and sig in _SPIN_REGRESS:
            return True
    return False


def _spin_oscillation(run, k):
    """True if the run contains a contiguous window of length >= k that strictly alternates between
    exactly two values (A-B-A-B), at least one of which is a regress signature — claim<->fail or
    claim<->release churn, never a forward working-state oscillation."""
    n = len(run)
    for i in range(n):
        j = i + 1
        while j < n and run[j] != run[j - 1] and (j - i < 2 or run[j] == run[j - 2]):
            j += 1
        window = run[i:j]
        if len(window) >= k and len(set(window)) == 2 and (set(window) & _SPIN_REGRESS):
            return True
    return False


def _spin_mode(run, k):
    """Classify the non-advancing run: 'identical' (k consecutive failed-finishes), 'oscillation'
    (A-B-A-B churn involving a regress), else None (below k, or exploring new states)."""
    if len(run) < k:
        return None
    if _spin_identical(run, k):
        return "identical"
    if _spin_oscillation(run, k):
        return "oscillation"
    return None


def spin_guard_adapter(events, k=_SPIN_K_DEFAULT):
    """Fire coherence:worker-spin (warn) for each (worker, task) whose trailing run of >=k
    non-advancing events — since its last state-frontier advance — is k consecutive identical
    events OR an A-B-A-B oscillation. A healthy trajectory (fewer than k, or any advance inside the
    window) stays quiet. Names the offending task in the id and the worker in the observed."""
    trails = {}
    order = []
    for ev in events or []:
        got = _spin_event_sig(ev)
        if got is None:
            continue
        worker, task, sig = got
        key = (worker, task)
        if key not in trails:
            trails[key] = []
            order.append(key)
        trail = trails[key]
        # dedup a status RE-SAVE (same status as the last trail sig): a field-edit that leaves the
        # status unchanged is not a transition, so not a spin step. Failed-finishes are NEVER deduped
        # — k consecutive of them IS the spin.
        if sig[0] == "status" and trail and trail[-1] == sig:
            continue
        trail.append(sig)
    viols = []
    for key in order:
        worker, task = key
        # the run of non-advancing signatures SINCE the last frontier advance (an advance resets it)
        run = []
        for sig in trails[key]:
            if _spin_is_advance(sig):
                run = []
            else:
                run.append(sig)
        mode = _spin_mode(run, k)
        if mode is None:
            continue
        short = str(task).rsplit(":", 1)[-1]
        viols.append(_v(
            "coherence:worker-spin:%s" % short, "warn",
            "no (worker, task) thrashes without advancing its state frontier",
            "%s has spun %s on %s: %d non-advancing events since last progress with no frontier advance"
            % (worker, mode, task, len(run)),
            "the worker advances the task or releases it", kind="logic",
            remediation="release the lease and let a fresh worker (or the operator) re-scope the task; "
                        "the poison-task breaker also opens on repeated failed-finishes"))
    return viols

