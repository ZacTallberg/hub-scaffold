# THE OPERATING AGREEMENT — hub-driven discipline

**Scope.** This agreement governs how every project in this organization is *operated* — by humans
and by agents alike. It is self-contained: everything needed to comply is stated here. It makes the
hub task board the **single, always-current source of truth** and commits every worker to working
solely off it.

> The one-line law: **No work happens that isn't a claimed hub task, and the board reflects
> reality at every moment.** If it's not on the board, it doesn't get built; the second reality
> changes, the board changes.

**Policy versus enforcement.** This agreement states the operating standard. The shipped Hub's
default `tracked` mode mechanically requires a claim, acceptance note, and evidence value, but it
does not dereference that evidence or require a verification command. Set
`HUB_DONE_STRICTNESS=strict` to make those two proof checks mechanical. Independent reviewer/canary
identity, deploy verification, alerting, and research discipline remain process or adopter-wired
controls unless the project supplies the named external gate. See `docs/ARCHITECTURE.md` for the
enforcement matrix and `SECURITY.md` before issuing a write token.
Strict completion strengthens proof of a claim; it does not reduce the authority of a token holder.

---

## §1 — The hub is the single source of truth

- The canonical record is the **event-sourced hub**: `PROJECT/.hub/events.jsonl` (append-only,
  hash-chained) is the write surface; everything readable — the `/hub` dashboard, its JSON
  surfaces, any generated `.md` views — is a **computed projection**, never hand-edited. Audit
  verdicts are recomputed, never written.
- A rendered task view (a generated `TASKS.md`, a dashboard table) is a view, not a scratchpad.
  To change a task you emit a hub event.
- The deployed `/hub` dashboard is how any human sees the board, the audit, and the history at any
  time. If the dashboard and reality disagree, that disagreement is itself a defect to fix first.

## §2 — The loop every unit of work runs

**DISCOVER → CLAIM → IMPLEMENT → RECORD → VERIFY.**

1. **DISCOVER** — read the next *unblocked, unclaimed* task from the hub, ranked by dependency and
   priority. Work is never picked by gut; you take the top of the queue.
2. **CLAIM** — mark it `in_progress` *before* touching anything. One task in progress per worker.
3. **IMPLEMENT** — do only that task. New work discovered mid-task is **added as a new task first**
   (§3); scope is never silently expanded.
4. **RECORD** — mark it `done` **with evidence**: a commit SHA, a passing-test transcript, a live
   URL, a screenshot. Evidence must be *dereferenceable* — a reviewer who was not present can
   follow it and re-check. Evidence must postdate the final edit; a run from before the last
   change is void. Run the Hub in `strict` mode when the server itself must enforce
   dereferenceability and a passing verification command.
5. **VERIFY proportionally** — use the smallest check that can catch the plausible failure. Routine
   low-risk work may need only a diff/evidence review or no executable test. Invoke a fresh,
   independent closer for releases, security/auth, migrations/destructive work, public contracts,
   concurrency/process launch, regressions, or occasional sampling. If a declared gate is red, the
   transition is refused and the task stays open.

The board is updated **at the moment of the event** — claim when claiming, done when proven —
never batched or reconstructed afterwards. A board that lags the work is itself a defect.

## §3 — Nothing off-list, ever

- Found a bug, a missing step, a new idea? **It becomes a task on the board first**, then it gets
  claimed. There is no "quick fix" lane; an off-board change is a discipline violation.
- Scope changes are tasks. Decisions are ADRs (§4). Research lands in the research log (§6).
  There is no fourth place for work to live.

## §4 — ADRs for every non-trivial decision (append-only)

- Every architectural, product, scope, or process decision becomes a numbered ADR in
  `PROJECT/ADR/`, recording the options weighed, the choice, and the why.
- Accepted ADRs are **immutable**. To change course, write a superseding ADR — never rewrite or
  delete — so the reasoning trail is never lost.
- A decision that was made but not recorded is treated as not made: it can be silently unmade by
  the next worker, which is exactly the failure ADRs exist to prevent.

## §5 — Definition of done: evidence proportional to consequence

- **FALSE-GREEN is the meta-failure** this agreement exists to kill: work that *reports* green
  without *being* green. Gates fail not by being absent but by being self-attested, bypassed,
  textual-only, or committed-but-not-deployed.
- Therefore, `done` always carries a truthful evidence pointer, but the evidence burden is
  **risk-proportional**. A small copy or formatting change does not require the full selftest
  ladder or a second agent. The implementer may provide routine evidence; never call that independent.
- **Identity separation is mandatory at declared independent gates, not on every task.** A release,
  deploy, privileged boundary, migration, public compatibility change, regression, or sampled audit
  uses a fresh verifier that did not build the work. The verifier reports once and exits; it does not
  linger as another worker or repair its own findings.
- Tests earn critical-path placement by defending a concrete failure mode. Broad batteries live off
  the ordinary edit loop and run at meaningful boundaries. Cheap impact-aware checks may remain on
  the default path.
- **Done ≠ merged, and done ≠ committed.** If a task's value requires a deploy, it is not done
  until the deployed artifact is verified live and the record names the live version (SHA).
- Any consumer of a gate artifact re-derives the verdict from the underlying data. A green flag
  contradicted by its own rows is fabricated-green and blocks everything downstream.

## §6 — Research before build

- No architectural work starts before its research is captured in `PROJECT/research/`: sources
  consulted, options weighed, the chosen path and why. A named technology in a directive is a
  hypothesis to validate, not a mandate.
- Project bootstrap is itself the first phase on the board: stand up the hub, seed the tasks,
  write the docs, arm the gates — *before* any feature task is claimed.

## §7 — PRECEDENCE: decide-and-go

- Default mode is **decide and go**: make the best-judgment call, record it (ADR if architectural,
  a pending-decision entry if genuinely owner-only), and proceed. Pausing to ask permission to
  continue already-directed work is a discipline violation.
- Stopping to ask is correct **only** for:
  1. a genuinely **irreversible or outward-facing fork** (data deletion, public announcement,
     spend commitment);
  2. a decision that **only the owner can make** — and even then it is queued as a pending
     decision while everything not blocked by it keeps moving;
  3. a **hard technical block**, stated precisely, after at least two distinct attempts;
  4. an **explicit hold** placed by the owner.
- **The newest directive wins.** When instructions conflict, the most recent owner directive
  supersedes older ones; note the supersession on the board rather than stalling to reconcile.

## §8 — Declared gates are out-of-process; ordinary work stays light

- A rule that the worker can self-attest is a suggestion, not an independent gate. Whenever this
  agreement declares a gate, it is backed by a mechanism **outside the worker's control**:
  - a **server-side push gate** (pre-receive) that rejects pushes violating repository law
    (e.g. credential-shaped files) where no client can skip it;
  - a **server-granted `done` transition** that requires a lease, acceptance note, and attached
    evidence; in strict mode it additionally requires dereferenceable evidence and a passing
    typed exit-0 verification receipt the worker produced;
  - an **audit** that recomputes invariants per request (served version matches HEAD, no
    mutations on read routes, no private data on public surfaces) and blocks `done` and deploy
    when red;
  - a **deployed-artifact canary** that proves the running system is built from the claimed
    version, out-of-band from the process that deployed it.
- **Every declared gate must have been SEEN to fire.** At the time the gate is written: seed a
  real positive, watch it refuse, confirm it stays quiet on a true negative, and record both runs
  as the receipt — leave no fixture file behind (this repo ships no battery for one to live in).
  The gate is invoked when its protected boundary is crossed. Gates are never weakened to make a
  task pass; weakening one is an ADR-level decision.

---

**In short:** the board is always true; every unit of work runs DISCOVER → CLAIM → IMPLEMENT →
RECORD → VERIFY; decisions are append-only ADRs; research precedes build; evidence and verification
are proportional to consequence; independent disposable closers protect meaningful boundaries;
workers decide-and-go except at genuinely irreversible forks; and declared gates are enforced by
something the worker cannot self-attest.
