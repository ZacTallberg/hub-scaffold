# THE OPERATING AGREEMENT — hub-driven discipline

**Scope.** This agreement governs how every project in this organization is *operated* — by humans
and by agents alike. It is self-contained: everything needed to comply is stated here. It makes the
hub task board the **single, always-current source of truth** and commits every worker to working
solely off it.

> The one-line law: **No work happens that isn't a claimed hub task, and the board reflects
> reality at every moment.** If it's not on the board, it doesn't get built; the second reality
> changes, the board changes.

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
   change is void.
5. **VERIFY** — an identity **other than the implementer** (a gate, an audit, a reviewer, a
   canary) confirms the claim. If the project is in an unsound state, the transition is refused
   and the task stays open.

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

## §5 — Definition of done: independently proven (FALSE-GREEN discipline)

- **FALSE-GREEN is the meta-failure** this agreement exists to kill: work that *reports* green
  without *being* green. Gates fail not by being absent but by being self-attested, bypassed,
  textual-only, or committed-but-not-deployed.
- Therefore: `done` = **independently proven with dereferenceable evidence**. No evidence → not
  done. Self-attested green is a defect even when the work happens to be correct.
- **The verifier identity must differ from the builder identity.** An agent may not grant its own
  gate. Acceptable verifiers: an out-of-process audit, a CI gate, a live canary that inspects the
  deployed artifact, or a human reviewer.
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

## §8 — Enforcement is out-of-process, or it is advisory

- A rule that the worker can self-attest is a suggestion, not a gate. Every gate in this agreement
  is backed by a mechanism **outside the worker's control**:
  - a **server-side push gate** (pre-receive) that rejects pushes violating repository law
    (e.g. credential-shaped files) where no client can skip it;
  - a **server-granted `done` transition** that requires attached evidence;
  - an **audit** that recomputes invariants per request (served version matches HEAD, no
    mutations on read routes, no private data on public surfaces) and blocks `done` and deploy
    when red;
  - a **deployed-artifact canary** that proves the running system is built from the claimed
    version, out-of-band from the process that deployed it.
- **Every gate must have failed in test.** A checker that has never fired on a seeded synthetic
  violation is presumed broken. Gates are never weakened to make a task pass; weakening a gate is
  an ADR-level decision.

---

**In short:** the board is always true; every unit of work runs DISCOVER → CLAIM → IMPLEMENT →
RECORD → VERIFY; decisions are append-only ADRs; research precedes build; done means independently
proven with evidence anyone can dereference; workers decide-and-go except at genuinely
irreversible forks; and every rule above is enforced by something the worker cannot self-attest.
