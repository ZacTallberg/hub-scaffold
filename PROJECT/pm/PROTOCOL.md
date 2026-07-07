# THE CAMPAIGN PROTOCOL — leader / worker / verifier / N-seat coordination (v2.1)

> canonical · owner: leader · update: by ADR + versioned amendment only — NEVER redefine protocol semantics in channel prose

Crystallized 2026-07-02 from a live-fire multi-agent campaign (referred to below as "v1") with
every learned failure baked in as law. Content-agnostic: seats, channels, and gates — no app
specifics. v2.1 adds §5's interrupt/preemption contract and §9 (steering & discipline).

---

## §0 Operating modes — when to activate this

| Mode | Seats | Activate when |
|---|---|---|
| **SOLO** (default) | one principal agent | normal work. pm/ stays dormant; continuity = `../HANDOFF.md` + hub |
| **PAIR** | LEADER + WORKER | a sustained queue where orchestration/verification and execution both saturate a session |
| **TRIAD** | + VERIFIER | the product makes checkable claims at scale, or ships need an independent fail-closed gate |
| **FLEET** | + WORKER-2..N / SPECIALIST(s) | independent workstreams that would serialize behind one worker |

Escalate one step at a time; every added seat costs coordination overhead — add a seat only when
its lane saturates. De-escalate (fold a seat back) the moment its lane dries up. Mode changes are
announced in DIRECTIVES + `../HANDOFF.md` §0.

## §1 Seats

| Seat | Owns | May never |
|---|---|---|
| **OPERATOR** (human) | doctrine, product direction, operator-only decisions; may post anywhere as `who: operator` (`OP-n`) | — (absolute authority; misrouted operator posts get a HOLD + re-route by the leader, not silent compliance) |
| **LEADER** (exactly 1) | orchestration · sequencing · issuing directives · **independent verification of every done** · stamps · CODE deploys · **the live ledger (§11)** · steering & discipline (§9) · answering blocked/question fast | delegate away verification-of-done; let the ledger/ADRs/docs lag the work layer even briefly |
| **WORKER** (1..N) | implementation · tests · migrations · DATA deploys (as actor-tagged) · publishing producer contracts (manifest) | CODE deploys; editing another seat's files; unscoped kill patterns; editing directives channels; deviating from a directive without a `proposal` |
| **VERIFIER** (0..1 standing) | independent verification per `../verify/README.md`: three-lane checks, verdicts, gate artifacts, `alert` escalations | deploys, ssh, app code, seeds/data patches, another seat's files; subagents if the operator has ordered watch-it-work |
| **SPECIALIST** (transient) | one scoped pass (design, security, migration) under a written charter with an explicit end condition | outliving its charter — it folds back (§12) |

**The verifier-identity invariant:** whoever verifies must not be whoever built. The leader
verifies the worker; the verifier checks the product; the gate re-derives the verifier. Nobody
stamps their own work.

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
(worker → code + `../verify/MANIFEST-CONTRACT.md` etc.; verifier → `../verify/**` minus the
contract). Writing outside your scope is an incident (v1 lost a producer contract to a verifier
overwrite). The leader's continuity file is `../HANDOFF.md` (there is no LEADER/DIRECTIVES.md —
the operator directs the leader).

## §3 Channel mechanics (hard rules)

The INVARIANTS below are law; the code snippets are the origin environment's reference
implementation (Windows · PowerShell 5.1 · Git Bash). Implement the same invariants with your
platform's native idioms, and record the binding in an ADR.

1. **Appends are atomic, lock-retrying, and never rewrite the file** — an editor-style rewrite
   changes the inode and silently kills every watcher, so editor tools are banned on channel
   files. Reference append (lock-retrying — shared files are lock-contended):
   ```powershell
   $f='<absolute path>'; $s="<content>`n"
   for($i=0;$i -lt 5;$i++){ try { [System.IO.File]::AppendAllText($f,$s); break } catch { Start-Sleep -m 400 } }
   ```
2. **Monitors must detect appends AND flag rewrites.** Naive tailing (`tail -f`/`-F`) misses
   in-place rewrites on some platforms — use whatever your environment provides that satisfies
   both (native file watchers, inotify, polling). Reference stat-polling watcher (emits new bytes
   on growth, flags shrink for a full re-read):
   ```
   F="<path>"; last=$(stat -c %s "$F" 2>/dev/null || echo 0); while true; do
     cur=$(stat -c %s "$F" 2>/dev/null || echo 0)
     if [ "$cur" -gt "$last" ]; then tail -c +$((last+1)) "$F"; last=$cur
     elif [ "$cur" -lt "$last" ]; then echo "[REWRITTEN - reread]"; last=$cur; fi; sleep 2; done
   ```
   Each seat arms its monitor on its INBOUND channel at spin-up, before any work.
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
| `ready` | — | seat online, monitor armed (once per session start) |
| `start` | — | task begun |
| `progress` | — | meaningful forward motion (not filler) |
| `done` | `evidence` (exact command + verdict-line output, post-final-edit) | completion CLAIM — credited only after leader verification (§6) |
| `deploy_request` | `kind: code|data`, `sha`/data-scope | done-that-needs-a-deploy names its deploy (DOCTRINE §2.3) |
| `deploy_done` | `kind`, `sha`, verification evidence | posted by the deploy owner after live verification |
| `blocked` | `tried: […]` (≥2 attempts) | hard blocker; poster moves to other work |
| `question` | — | decision/help request; poster MOVES ON meanwhile |
| `proposal` | what + why + the alternative | request to deviate from a directive or improve the plan — posted BEFORE deviating, always; leader adjudicates on the seat's channel |
| `finding` | grounded evidence | a discovery that changes the plan's premises (leader converts to note/gap/task — live, §11) |
| `heartbeat` | real counts/position | ≥ every 15 min during long work; numbers, not vibes |
| `alert` | grounded evidence | verifier finding escalation (§8) |
| `gate_result` | `artifact` path, `green` bool, `rule` id | gate artifact written (consumers re-derive, never trust) |
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
- **Defect directives follow the six-part template** (DOCTRINE §3):
  1. DEFECT — instance, grounded (id + rendered-vs-evidence + quote)
  2. ROOT — which code path emitted it
  3. CLASS FIX — the class-wide detector + self-test
  4. CLASS QUERY — the count of siblings ("the found instance is never the only one")
  5. IN-PLACE FIX — drain the stock
  6. PROBE — bank the never-again test/eval row
- **Acceptance criteria are mechanical** — a command and its expected verdict line, never adjectives.
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

- **Interruptible points:** seats re-check their inbound monitor between atomic units and at
  least every ~10 minutes inside long units. A single tool operation is never interrupted
  mid-flight (atomicity) — which is why kills must be SHA/PID-scoped (§7.3).
- **Operator interrupts outrank everything** (§1 authority chain): an `OP-` post in any channel
  preempts like `🔴🔴`; the leader reconciles afterward (HOLD + re-route if misrouted).
- **Steering is cheap by design:** because every seat checkpoints into `STATE.md`, the leader
  (or operator) can redirect any seat at any time and lose at most one atomic unit of work.

## §6 Verification & credit (the leader's core duty)

1. A `done` is a **claim**. The leader independently: re-runs the stated verification extracting
   the explicit verdict line (match `^(OK|FAILED)|Ran \d+ tests` — never trust the last stdout
   line; test chatter fools tail-grabs), reads the implementing diff, and for user-facing work
   checks the live surface itself.
2. **Evidence freshness:** the run must postdate the final edit, and must exercise the REAL chain
   (v1's deploy #2 looped prod 11 minutes because tests validated a middleware, not the proxy
   chain in front of it — test the chain, not the unit, before ships).
3. **Parse actual shapes.** Instruments lie by key-drift (`entities` vs rows, `data` vs `payload`
   — three broken instruments shipped in v1). Prefer raw reads over assumed schemas when verifying.
4. Credit = the `Leader-verified:` line. Mis-credits are retracted by a `correction` + channel note
   — including the leader's own (self-verification errors get the same treatment).
5. **Done ≠ live** (DOCTRINE §2.3): a verified done that needs a deploy stays open until its
   `deploy_done` lands and is live-verified.

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

## §8 The independent verification lane

Wiring for the TRIAD+ modes (contract: `../verify/README.md`):
1. Worker publishes the manifest + contract; verifier sweeps; gate artifacts block data ships fail-closed.
2. **Ship sequence (locked):** worker posts wave-green `done` → leader verifies + runs CODE deploy
   → worker regenerates manifest + publishes the ship's changed-record list → verifier runs the
   fresh delta → gate green (re-derived) + **leader stamp** → worker runs the DATA deploy.
3. **Stamps:** a gate artifact is provisional until the leader appends `Leader-verified:` on the
   verifier's channel. The stamp is the authorization primitive.
4. **Auto-routing:** alerts for ESTABLISHED failure-mode classes flow verifier → worker directly
   (the worker consumes `alert` events; the leader is not a relay). The leader keeps: novel
   classes, escalations, ambiguity, all stamps, and deploy triggers.
5. The verifier is authorized to distrust gathered data and check the live world; snapshots make
   live checks count.

## §9 Steering & discipline (how the leader keeps seats in line)

### §9.1 Leader cadence
- **Continuously:** monitor armed; `blocked`/`question`/`proposal` answered within minutes (an
  unanswered blocker is a leader defect); dones verified as they land (§6); ledger live (§11).
- **Per ship:** gate + stamp + live verification (§7/§8).
- **Per session (and at least daily):** a ledger-parity sweep (created-vs-transitioned, stale
  `in_progress`); backlog re-prioritized against the CHARTER; `../HANDOFF.md` re-cut; **one
  spot-audit of intermediate work per active seat** — sample the middle of the work, not just the
  dones (v1's fabrication was caught by mechanical audit of in-flight output, not by completion review).

### §9.2 Drift detection (what the leader watches for)
- **Acceptance drift** — output solves a neighboring problem, not the directive's.
- **Scope drift** — work beyond the directive without a `proposal`.
- **Quality drift** — evidence getting thinner, verification commands getting weaker, prose
  replacing numbers in heartbeats.
- **Behavioral drift** — write-scope violations, filler traffic, unscoped operations, banned-topic
  narration.
Signals: the bus tail, diff reads, spot-audits, and the gate's own counters (a verifier whose
supported-rate jumps discontinuously is drifting; v1 calibration anchors exist for exactly this).

### §9.3 The discipline ladder (proportional, always on the seat's own channel)
1. **NUDGE** — an inline note in the next routine directive. No ceremony.
2. **CORRECTION** — a numbered directive naming the drift, the exact rule violated
   (PROTOCOL/DOCTRINE §), and the required behavior. Acknowledged by action, not by an ack event.
3. **CHARTER AMENDMENT** — the same drift twice means the charter was ambiguous: supersede the
   charter version with the boundary made explicit (v1's verifier went through four charter
   versions — that churn is the ladder *working*).
4. **SEAT RESET** — for fabrication, repeated hard-boundary violations, or unrecoverable
   confusion: archive the seat's channel + charter whole, `void` tainted outputs, spin up a fresh
   charter + session (§12), and re-verify anything the old seat produced before reuse. Two resets
   of the same seat design = the design is wrong — re-architect the seat (narrow its scope, add
   tooling, or split it) instead of resetting a third time.
CORRECTION and above are recorded live (§11): an incident row if the drift produced defects, and
the pattern goes to `../registers/FAILURE-MODES.md` group H if it's new.

### §9.4 Watchdogs & liveness
- **Silence watchdog:** heartbeat window = 15 min (or the seat's declared cadence). Silence past
  2 windows → the leader posts a `🔴` liveness-check directive; silence past 1 more → the seat is
  presumed dead: expire its claims, salvage-and-verify whatever is on disk, respawn or reassign
  (§12). Nothing is voided on death alone — dead seats' work is verified, not discarded.
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
- **No `done` without verification** — the leader's verify pass (§6) and the hub transition
  (`done` + `verified_by` + evidence) are one atomic act; likewise `blocked` ⇒ deps recorded.
- **No deploy without its entity** — appended by the act of deploying, `audit_ok` computed.
- **Doctrine born in traffic** → `../DOCTRINE.md` §6 + ADR before the traffic moves on; defect
  classes → FAILURE-MODES + INCIDENTS at classification time; research → `../research/` +
  chronicle entry the session it lands; `../HANDOFF.md` re-cut at every significant state change.

Governance parity is audited, not assumed: hub transitions must track real work in real time
(v1: 221 tasks created, 14 transitioned, ADR stubs of 15 bytes — the governance layer was fiction
while the work layer was real; that is an `FM-H` incident). A leader who lets the ledger lag is
failing the seat's core duty, whatever else is getting done.

## §12 Seat lifecycle

- **Spin-up:** leader writes `seats/<SEAT>/CHARTER.md` (role, boundaries, write scope, deploy
  ownership, current assignment) → creates the seat's `DIRECTIVES.md` with directive -001 →
  seat session starts: reads PROTOCOL + charter + DIRECTIVES tail + `../HANDOFF.md`, arms its
  monitor, posts `ready`, begins.
- **Extra seats:** copy the WORKER charter shape; unique seat id (`WORKER-2`, `SPECIALIST-DESIGN`);
  disjoint write scopes ALWAYS.
- **Replacement / supersession:** a seat that must be re-chartered gets a WHOLE new charter
  version; the old charter + directives are archived intact to `archive/` — never edited.
- **Fold-back (spin-down):** the seat's final `STATE.md` + a closing directive record what it
  owned; unabsorbed work returns to the backlog explicitly; its scope reverts by charter note.
- **Leader handoff:** outgoing leader updates `../HANDOFF.md`, posts a deploy-HOLD directive to
  every seat, ends. Incoming leader reads HANDOFF + all channel tails, arms monitors, posts the
  hold-lift. Numbering and doctrine continue unbroken — the campaign survives any single session.

## §13 Bus evolution

This file-based bus (append-only files + stat-poll monitors + lock-retry appends) is the proven
floor, chosen because sessions cannot message each other directly. If a real addressable bus with
per-seat ACLs becomes available, adopt it by ADR — the event vocabulary (§4) and duties transfer unchanged.
