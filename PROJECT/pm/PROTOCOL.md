# THE CAMPAIGN PROTOCOL — leader / worker / verifier / N-seat coordination (v2.2)

> canonical · owner: leader · update: by ADR + versioned amendment only — NEVER redefine protocol semantics in channel prose

Crystallized 2026-07-02 from a live-fire multi-agent campaign (referred to below as "v1") with
every learned failure baked in as law. Content-agnostic: seats, channels, and critical boundaries — no app
specifics. v2.2 makes real-operation completion the default, makes all exceptional probes
transient, and makes receipts compose without verification fan-out.

---

## §0 Operating modes — when to activate this

| Mode | Seats | Activate when |
|---|---|---|
| **SOLO** (default) | one principal agent | normal work. pm/ stays dormant; continuity = `../HANDOFF.md` + hub |
| **PAIR** | LEADER + WORKER | a sustained queue where orchestration and execution both saturate a session |
| **TRIAD** | + transient VERIFIER | a declared critical boundary needs one independent receipt; fold the seat immediately after it reports |
| **FLEET** | + WORKER-2..N / SPECIALIST(s) | independent workstreams that would serialize behind one worker |

Escalate one step at a time; every added seat costs coordination overhead — add a seat only when
its lane saturates. De-escalate (fold a seat back) the moment its lane dries up. Mode changes are
announced in DIRECTIVES + `../HANDOFF.md` §0.

## §1 Seats

| Seat | Owns | May never |
|---|---|---|
| **OPERATOR** (human) | doctrine, product direction, operator-only decisions; may post anywhere as `who: operator` (`OP-n`) | — (absolute authority; misrouted operator posts get a HOLD + re-route by the leader, not silent compliance) |
| **LEADER** (exactly 1) | orchestration · sequencing · issuing directives · risk classification and boundary-verifier dispatch · stamps · CODE deploys · **the live ledger (§11)** · steering & discipline (§9) · answering blocked/question fast | call implementer evidence independent; let the ledger/ADRs/docs lag the work layer even briefly |
| **WORKER** (1..N) | implementation · migrations · product/data delivery · DATA deploys (as actor-tagged) · actual-operation receipts | CODE deploys; permanent tests/fixtures/checker workflows; editing another seat's files; unscoped kill patterns; editing directives channels; deviating from a directive without a `proposal` |
| **VERIFIER** (transient only) | one explicitly scoped critical-boundary operation or disposable probe per `../verify/README.md`, its durable receipt, any `alert`, then exit | standing verification; permanent probes/fixtures/workflows; deploys, ssh, app code, seeds/data patches, or another seat's files |
| **SPECIALIST** (transient) | one scoped pass (design, security, migration) under a written charter with an explicit end condition | outliving its charter — it folds back (§12) |

**The boundary-verifier identity invariant:** when work crosses a declared critical independent
gate, whoever verifies must not be whoever built. Routine work remains in SOLO; copy, style,
animation polish, and non-critical changes never activate a verifier. The temporary verifier exits
as soon as its receipt lands.

**The authority chain:** OPERATOR > DOCTRINE/CHARTER > LEADER directives > backlog order. A seat
that believes a directive violates DOCTRINE or the CHARTER must say so (`question`/`proposal`)
before executing — obedience is not a defense for shipping a violation.

## §2 Topology & write ACL

```
pm/
  PROTOCOL.md                 this law
  STATUS.jsonl                shared bus: ALL seats append events (multi-writer, lock-retry only)
  deploy.lock                 deploy mutex (§7) — present only while a deploy runs
  seats/<SEAT>/
    CHARTER.md                role + boundaries + current assignment (leader-authored; versioned, superseded whole)
    DIRECTIVES.md             leader → seat, append-only, numbered <seat-prefix>-NNN
    STATE.md                  the seat's resumable position (seat-owned; rewritten in place)
  archive/                    superseded charters/directives, whole files, dated
```

**One writer per file** — the only multi-writer file is `STATUS.jsonl`. The leader writes charters
and directives; each seat writes only its own `STATE.md` and its designated product dirs
(worker → product code/data; transient verifier → its scoped receipt and disposable scratch).
Writing outside your scope is an incident (v1 lost a producer contract to a verifier
overwrite). The leader's continuity file is `../HANDOFF.md` (there is no LEADER/DIRECTIVES.md —
the operator directs the leader).

## §3 Channel mechanics (hard rules)

The INVARIANTS below are law; the code snippets are the origin environment's reference
implementation (Windows · PowerShell 5.1 · Git Bash). Implement the same invariants with your
platform's native idioms, and record the binding in an ADR.

1. **Appends are atomic, lock-retrying, and never rewrite the file** — a rewrite invalidates
   cursor continuity and the durable transcript, so editor tools are banned on channel files.
   Reference append (lock-retrying — shared files are lock-contended):
   ```powershell
   $f='<absolute path>'; $s="<content>`n"
   for($i=0;$i -lt 5;$i++){ try { [System.IO.File]::AppendAllText($f,$s); break } catch { Start-Sleep -m 400 } }
   ```
2. **Delivery is pushed, cursor-ordered, and rewrite-aware.** The addressable bus publishes every
   committed append to the inbound seat immediately. A seat subscribes before work begins, folds
   from its last durable cursor on reconnect, rejects duplicates, and flags a hash/cursor rewrite.
   Files may retain the transcript, but interval polling, periodic tailing, and manual sync are not
   live coordination. Transport truth is always **Connected** or **Disconnected**.
3. **Numbering:** re-read the channel tail immediately before appending; next id = last+1. A
   collision/skip gets a `CORRECTION` block — ids are never reused or renumbered. Sub-numbers
   (`W1-014.1`) for patches to an in-flight directive.
4. **Re-read before acting.** Multiple sessions share the tree; expect files to change under you;
   never revert another seat's changes.

## §4 STATUS.jsonl — the event bus

One JSON object per line. Required: `ts` (ISO, ONE timezone campaign-wide — mixed clocks caused a
false leader callout in v1), `who` (seat id), `type`, `task`, `detail`. Event types:

| type | Required extras | Semantics |
|---|---|---|
| `ready` | — | seat online, push subscription connected (once per session start) |
| `start` | — | task begun |
| `progress` | — | meaningful forward motion (not filler) |
| `done` | `evidence` (real operation + observed outcome; critical probe receipt only when used) | completion record — credited by the leader under §6 |
| `deploy_request` | `kind: code|data`, `sha`/data-scope | done-that-needs-a-deploy names its deploy (DOCTRINE §2.3) |
| `deploy_done` | `kind`, `sha`, observed live outcome | posted by the deploy owner after the real deploy operation |
| `blocked` | `tried: […]` (≥2 attempts) | hard blocker; poster moves to other work |
| `question` | — | decision/help request; poster MOVES ON meanwhile |
| `proposal` | what + why + the alternative | request to deviate from a directive or improve the plan — posted BEFORE deviating, always; leader adjudicates on the seat's channel |
| `finding` | grounded evidence | a discovery that changes the plan's premises (leader converts to note/gap/task — live, §11) |
| `heartbeat` | real counts/position | ≥ every 15 min during long work; numbers, not vibes |
| `alert` | grounded evidence | verifier finding escalation (§8) |
| `gate_result` | durable receipt reference, observed outcome, boundary id | exceptional critical-boundary result; no standing artifact generator |
| `preempted` | paused task + resume point | checkpoint acknowledgment of an interrupt/halt (§5) |
| `halt` | scope (`seat`/`campaign`) + reason | all-stop marker; only the issuer lifts it, by numbered directive |
| `void` | artifact/rows voided + reason | tamper-evident invalidation (`../README.md` §5) |
| `directive` | — | operator order (`who: operator`) |
| `correction` | what it corrects | supersedes an earlier event by reference |

**Banned traffic:** "ready to X" idling, permission-seeking, ack-only events for routine
directives (act instead; the directive log + your `start` event is the ack), and context/window/
compaction narration — continuity is `STATE.md`'s job (v1 spent five directives fighting this; the
cure is structural, not disciplinary).

## §5 Directives & interrupts (leader → seat)

### Directive anatomy
- **Header:** `**<SEAT-PREFIX>-NNN — <TITLE>**` + urgency marker + source (`operator verbatim:
  "…"` when elevating operator words).
- **Defect directives follow the five-part repair template** (DOCTRINE §3):
  1. DEFECT — instance, grounded (id + rendered-vs-evidence + quote)
  2. ROOT — which code path emitted it
  3. REPAIR TASK — fresh Hub task and route, including the dedicated repair lane when available
  4. FIX — restore the causal path and any already-known affected stock
  5. RETRY — repeat the real failed operation and record the outcome
- **Acceptance criteria name an observable outcome.** A command is optional, and copy, wording,
  style, motion, and other non-critical work must not acquire a validation command.
- **Every deploy step carries `actor:`** — a step tagged for another seat is a wait-for-signal, not an action.
- **Directives override the backlog on conflict**; the leader records WHY in the directive.
- **Answers to `question`s/`proposal`s** are appended to the same channel, referencing the event.
- Completion credit is appended inline: `**Leader-verified: <task>** (<evidence>)` — the channel
  doubles as the credit ledger.

### Urgency, preemption & halt (the interrupt contract)
| Marker | Meaning | Seat obligation |
|---|---|---|
| *(none)* | queue order | pick up per sequencing |
| `🔴` URGENT | interrupt at the next safe point | finish the current atomic unit, checkpoint `STATE.md`, post `preempted` (what paused + resume point), comply, then resume from the checkpoint |
| `🔴🔴` DROP-EVERYTHING | comply immediately, mid-task | reserved for live user-facing harm, data-loss risk, security, or deploy collision; checkpoint after complying |
| `🛑 HALT` | all-stop (seat- or campaign-scoped) | checkpoint, post `preempted`, post/watch `halt`, do NOTHING in scope until the issuer lifts it by numbered directive |

- **Interruptible points:** seats consume pushed inbound directives between atomic units and at
  least every ~10 minutes inside long units. A single tool operation is never interrupted
  mid-flight (atomicity) — which is why kills must be SHA/PID-scoped (§7.3).
- **Operator interrupts outrank everything** (§1 authority chain): an `OP-` post in any channel
  preempts like `🔴🔴`; the leader reconciles afterward (HOLD + re-route if misrouted).
- **Steering is cheap by design:** because every seat checkpoints into `STATE.md`, the leader
  (or operator) can redirect any seat at any time and lose at most one atomic unit of work.

## §6 Completion evidence & credit (the leader's core duty)

1. **The real operation is the default proof.** The worker performs the changed behavior and
   records the observed result. If it works and no critical boundary remains, the leader marks the
   task done and stops. Copy, wording, style, animation polish, and other non-critical work receive
   no test, automated copy validation, closer, or second pass.
2. **Tests never accumulate.** Do not add permanent test files, fixtures, checker scripts,
   calibration sets, scheduled runs, or CI verification workflows. Security, destructive-data,
   migration, protocol-compatibility, and concurrency boundaries are the rare exceptions that may
   justify one temporary probe.
3. **A critical probe is disposable.** Create it in system temporary space or explicitly disposable
   task scratch, run it once after the final edit, retain its command/scope/outcome receipt, delete
   it before commit, and fold the verifier seat. The probe is not product code.
4. **Receipts compose.** A completed dependency's receipt is inherited. Parent tasks and releases
   never replay child proof; they observe only a new critical integration seam created by joining
   those completed parts. Verifier-of-verifier fan-out is forbidden.
5. **Real failure is sufficient notice.** When the actual operation breaks, create a fresh repair
   task, route it to a dedicated error-fixing lane when available, and let delivery agents continue
   unrelated work. The successful retry closes the repair task.
6. **Done ≠ live** (DOCTRINE §2.3): work that needs a deploy stays open until its `deploy_done`
   records the observed live outcome.

## §7 Deploy interlocks (code, not prose)

1. **Ownership is split and absolute** (set per campaign in charters; default: CODE = leader,
   DATA = worker) — and **code-first** when a change spans both.
2. **Mutex:** before any deploy, create `pm/deploy.lock` =
   `{actor, kind, sha, started}`; remove on completion. A present lock = NO concurrent deploy of
   any kind (v1's documented stuck-build trap) — wait or escalate, never race.
3. **Scoped kills only:** any kill pattern names a specific SHA/tag/PID, never a command shape
   (a v1 worker's bare kill-by-command-shape nearly murdered the leader's deploy).
4. **Patient canaries:** know the platform's slow stages; a "hung" deploy is usually the slow
   release stage. A predeploy failure means the old artifact still serves — check before panicking.
5. Every deploy appends a hub `deploy` entity (SHA+timestamp, unconditional) and a `deploy_done` event.

## §8 The independent verification lane (boundary-triggered)

This lane exists only for an explicitly named critical boundary (contract:
`../verify/README.md`). It is never activated for copy, style, motion, routine fixes, broad
sampling, or release ceremony:
1. The leader names the precise security, destructive-data, migration, protocol, or concurrency
   seam and why the real operation alone cannot expose unacceptable failure.
2. The verifier performs the protected real operation when safe; only when necessary, it creates
   one disposable probe in temporary scratch and runs it once.
3. The verifier appends a durable receipt with scope, command/action, observed outcome, target SHA,
   and identity, deletes all probe/fixture/scratch artifacts before commit, then exits.
4. Completed child receipts are inherited. A release receipt covers only a newly created critical
   integration seam and never expands into nested verifier fan-out.
5. A failure opens a fresh repair task and may auto-route to a dedicated repair worker. After the
   repair, retry the failed real operation; do not install a standing regression workflow.

## §9 Steering & discipline (how the leader keeps seats in line)

### §9.1 Leader cadence
- **Continuously:** push subscription connected; `blocked`/`question`/`proposal` answered within minutes (an
  unanswered blocker is a leader defect); evidence recorded as work lands (§6); ledger live (§11).
- **Per ship:** perform the actual ship and record the observed live outcome; invoke §8 only for a
  newly created critical integration seam.
- **Per session (and at least daily):** reconcile the live ledger with actual starts/completions,
  recover stale `in_progress` ownership, reprioritize the backlog against the CHARTER, and re-cut
  `../HANDOFF.md`. This is queue maintenance, not a rerun of completed work.

### §9.2 Drift detection (what the leader watches for)
- **Acceptance drift** — output solves a neighboring problem, not the directive's.
- **Scope drift** — work beyond the directive without a `proposal`.
- **Throughput drift** — agents adding non-critical checks, validators, or review fan-out after
  changed behavior already works; prose replacing numbers in heartbeats.
- **Behavioral drift** — write-scope violations, filler traffic, unscoped operations, banned-topic
  narration.
Signals: the bus tail, observable task movement, repeated failed real operations, and accumulated
validation artifacts. A queue growing while agents repeatedly check completed work is drifting.

### §9.3 The discipline ladder (proportional, always on the seat's own channel)
1. **NUDGE** — an inline note in the next routine directive. No ceremony.
2. **CORRECTION** — a numbered directive naming the drift, the exact rule violated
   (PROTOCOL/DOCTRINE §), and the required behavior. Acknowledged by action, not by an ack event.
3. **CHARTER AMENDMENT** — the same drift twice means the charter was ambiguous: supersede the
   charter version with the boundary made explicit (v1's verifier went through four charter
   versions — that churn is the ladder *working*).
4. **SEAT RESET** — for fabrication, repeated hard-boundary violations, or unrecoverable
   confusion: archive the seat's channel + charter whole, `void` tainted outputs, spin up a fresh
   charter + session (§12), and reconcile affected work through its real operation before reuse. Two resets
   of the same seat design = the design is wrong — re-architect the seat (narrow its scope, add
   tooling, or split it) instead of resetting a third time.
CORRECTION and above are recorded live (§11): an incident row if the drift produced defects, and
the pattern goes to `../registers/FAILURE-MODES.md` group H if it's new.

### §9.4 Watchdogs & liveness
- **Silence watchdog:** heartbeat window = 15 min (or the seat's declared cadence). Silence past
  2 windows → the leader posts a `🔴` liveness-check directive; silence past 1 more → the seat is
  presumed dead: expire its claims, salvage the scoped work, reconcile it through the real
  operation, then respawn or reassign (§12). Nothing is voided on death alone—unfinished work
  returns to the task or repair queue.
- **Anti-thrash watchdog:** the same task failing twice on the bus triggers a stop-work +
  re-architecture directive (DOCTRINE §1.4). There is never a third identical attempt.
- **Runaway watchdog:** high traffic with non-moving counts (heartbeats without progress) draws a
  CORRECTION + a narrowed scope.

### §9.5 Steering upward (how seats push back and redirect the campaign)
- **`proposal` before deviation, always** — no silent improvements, no surprise architecture. The
  leader adjudicates fast: accept ⇒ a directive amendment (the deviation becomes law); reject ⇒
  reasons on the channel (and "rejected" is recorded — it is anti-rework armor).
- **`finding` when premises change** — a discovery that invalidates the plan is posted the moment
  it's grounded; the leader converts it live into note/gap/task and re-sequences.
- **Challenge duty** (§1): a directive that violates DOCTRINE/CHARTER is challenged before
  execution. The operator can steer ANY seat directly at any time (§5); seats never have to choose
  between the leader and the operator — the operator wins, and the leader reconciles the record.

## §10 Escalation & autonomy

Two attempts then `blocked` with `tried`; ~20-min timebox on rabbit holes; `question`-then-move-on;
anti-stall caps in bulk sweeps; operator-only forks → `../registers/DECISIONS-PENDING.md` with a
recommendation + default, and route around. The leader answers `blocked`/`question` within minutes
— an unanswered blocker is a leader defect.

## §11 The live-ledger law (channels are not a governance store)

**The hub is THE source of truth, and the LEADER is personally, non-delegably accountable for
keeping it — and every ADR and document — updated LIVE, with full perfectionistic effort.** The
pm channels are operational traffic only; the ledger is the record.

The cadence is per-event, never batched:
- **No directive without a task** — issuing a directive creates/claims its hub task (`in_progress`) in the same act.
- **No decision without an ADR** — recorded when the decision is made, with real prose (a stub entity is a defect).
- **No `done` without completion evidence** — the real operation and observed outcome (§6) land
  with the hub transition (`done` + `verified_by` + evidence). A transient critical receipt is
  attached only when that boundary actually required one; likewise `blocked` ⇒ deps recorded.
- **No deploy without its entity** — appended by the act of deploying, `audit_ok` computed.
- **Doctrine born in traffic** → `../DOCTRINE.md` §6 + ADR before the traffic moves on; observed
  failures → a fresh task + INCIDENTS, and useful repeated/novel classes → FAILURE-MODES; research → `../research/` +
  chronicle entry the session it lands; `../HANDOFF.md` re-cut at every significant state change.

Governance parity is audited, not assumed: hub transitions must track real work in real time
(v1: 221 tasks created, 14 transitioned, ADR stubs of 15 bytes — the governance layer was fiction
while the work layer was real; that is an `FM-H` incident). A leader who lets the ledger lag is
failing the seat's core duty, whatever else is getting done.

## §12 Seat lifecycle

- **Spin-up:** leader writes `seats/<SEAT>/CHARTER.md` (role, boundaries, write scope, deploy
  ownership, current assignment) → creates the seat's `DIRECTIVES.md` with directive -001 →
  seat session starts: reads PROTOCOL + charter + DIRECTIVES tail + `../HANDOFF.md`, connects its
  push subscription, posts `ready`, begins.
- **Extra seats:** copy the WORKER charter shape; unique seat id (`WORKER-2`, `SPECIALIST-DESIGN`);
  disjoint write scopes ALWAYS.
- **Replacement / supersession:** a seat that must be re-chartered gets a WHOLE new charter
  version; the old charter + directives are archived intact to `archive/` — never edited.
- **Fold-back (spin-down):** the seat's final `STATE.md` + a closing directive record what it
  owned; unabsorbed work returns to the backlog explicitly; its scope reverts by charter note.
- **Leader handoff:** outgoing leader updates `../HANDOFF.md`, posts a deploy-HOLD directive to
  every seat, ends. Incoming leader reads HANDOFF + all channel tails, connects subscriptions, posts the
  hold-lift. Numbering and doctrine continue unbroken — the campaign survives any single session.

## §13 Bus evolution

The required live transport is an addressable push bus with per-seat identity, ordered cursors,
and reconnect catch-up. Append-only files may remain its durable history when useful, but are never
polled as the normal coordination loop. If the bus is unavailable, coordination is explicitly
**Disconnected** until it recovers; the event vocabulary (§4) and duties remain unchanged.
