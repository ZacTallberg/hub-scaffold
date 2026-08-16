# MAINTAIN — reconcile code ↔ hub ↔ live ↔ docs

Keep a project *honest and current* over time. This campaign catches the drift that accumulates
silently: the hub board falling behind the code, docs claiming things that shipped-then-changed, a
"done" task whose evidence no longer resolves, a live site running a different build than HEAD. Run it
when an observed inconsistency creates a maintenance task, before a consequential release, or when
resuming a project cold. It is an on-demand repair campaign, not a standing validation loop.

Read `00-orchestration-method.md` first. Substitute `{{REPO_PATH}}`, `{{LIVE_URL}}`, `{{DEPLOY_CMD}}`.

---

## Prompt A — the coherence audit (one agent, read-only)

> Audit whether `{{REPO_PATH}}` is COHERENT: does the code, the hub board, the docs, and the live site
> agree? STRICT read-only. Check, and report each mismatch with evidence:
> 1. **deployed == HEAD** — committed short SHA vs the `<meta name="build">` the live site serves at
>    `{{LIVE_URL}}`. A mismatch means undeployed commits or an unstamped deploy — say which.
> 2. **board == reality** — every hub task marked `done` still has a receipt/evidence reference that
>    resolves; do not rerun its completed work or optional `verification_command`. Confirm every open
>    task is still real and identify work visible in recent commits that has NO task (the board is
>    behind the code). Child receipts compose into their parent/release record.
> 3. **docs == behavior** — plane docs, README, and load-bearing code COMMENTS that describe behavior
>    the code no longer has; aspirational/planned items written as if done; dead `file:line` references.
> 4. **false-green residue** — any gate/audit that can pass while the thing it checks is broken; any
>    `audit_ok`/status that is hand-written rather than computed; a coherence check that goes green when
>    an input is missing (it must say "unknown", never silently pass).
> Return structured findings `{kind, severity, file, claim, reality, evidence}`. Do NOT fix, create
> tests, or validate copy/style — report only discrepancies that change truth or operation.

Before recording a critical or high mismatch, re-open only its cited source and real surface to make
sure the factual basis is present. This is claim hygiene, not a second verification stage.

## Prompt B — regenerate the state anchor (the resume-safety net)

Hand-written state snapshots drift and contradict each other; a *generated* one can't. Give a project
a single ground-truth resume anchor that any cold session reads first:

> Write / regenerate `RESUME.md` at the project root from GROUND TRUTH ONLY (never hand-edited values):
> git HEAD short SHA; dirty-file count; the live build SHA from `{{LIVE_URL}}`'s `<meta name="build">`;
> the last few board events / directive numbers (grep the append-only channels, skipping any
> stall/heartbeat noise lines); and a paused/active flag from an explicit flag file. Emit a small table
> + a NOTE if live != HEAD (say whether the delta is docs-only or app code). Then stamp every OTHER
> hand-written snapshot doc with "superseded-by RESUME.md for STATE; keep for doctrine only". Wire this
> to run on every deploy and every pause so the anchor is never stale.

## Prompt C — long-campaign reconciliation (when several state docs disagree)

If a multi-session campaign has fragmented across snapshots (a handoff says one SHA, a paused-state doc
another, the channel a third, git a fourth, the live build a fifth):

> Establish the ONE true current state from ground truth (git HEAD, the live build SHA, the append-only
> event log's last entries, the working-tree dirt) and reconcile every disagreeing snapshot to it.
> Where a "held / not deployed" claim conflicts with the live build, VERIFY via git ancestry whether
> the commits are actually in the live build before trusting either doc. Correct the durable record and
> the resume anchor; do not delete the old snapshots, stamp them doctrine-only. Route any watchdog /
> heartbeat automation to its own log so it can't flood the event channels.

## Prompt D — event-driven repair lane

Do not install a standing scanner, scheduled audit, or automatic test pipeline. Actual use, deployment,
and operations are the default sensors. When one exposes a defect or coherence gap, create a focused
repair-lane task with the observed failure as evidence and route it immediately. If the failure crosses
a critical security, destructive/data-integrity, migration, protocol-compatibility, or concurrency
boundary, a worker may create one transient probe in temporary space, run it once, record the receipt,
and delete the probe before commit. Everything else is fixed by performing the real operation again.

## Discipline

MAINTAIN reports and reconciles the *record*; it does not silently fix code. A real defect it surfaces
becomes a hub task and flows through `feature-buildout.md`. The one thing it DOES write directly is
the generated state anchor and the reconciled snapshots — because those are ground-truth projections,
not opinions. `verification_command` remains optional in both tracked and strict modes. A completed
task's receipt is durable evidence, not an invitation to repeat its proof; parent tasks inherit child
receipts and inspect only a genuinely new integration seam. Stop as soon as the requested real-world
operation succeeds and no critical boundary requires a disposable probe.
