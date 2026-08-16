# THE PROJECT PLANE — BOOTSTRAP SPECIFICATION (v1.1, 2026-08-03)

Every generated plane embeds `PROJECT/HUB-QUALITY.md`, making exceptional Hub construction a
birthright rather than optional post-generation guidance.

**This document is a self-contained specification and template set.** Hand it to a fresh agent in
any environment and it can create the Project Plane without origin-specific files or credentials.
The templates include contracts for independent verification, runs, deploys, and multi-agent
coordination; those controls become active only when the adopting project implements and wires their
runners. The `hub-scaffold` repository supplies a working filesystem Hub/Django reference binding;
this standalone document embeds the plane templates, not that implementation's source code.

Provenance: crystallized from a live-fire multi-agent campaign (2026-07) and a five-project
quality-doctrine corpus. Template blocks below are machine-inserted from the
canonical files by `tools/build_bootstrap.py` (`--check` verifies zero drift) — treat them as
byte-exact.

---

## §0 How to use this document (instructions to the bootstrapping agent)

1. Read §1–§3 to load the laws and the architecture. Do not skip to the files.
2. Execute §6 (the bootstrap procedure), instantiating the templates in §4 **verbatim** — they are
   content-agnostic by construction; anything project-specific is a fill-in marked `<...>`.
3. **Use your environment's native mechanics.** Wherever this spec or the embedded templates show
   concrete commands (append snippets, file watchers, hashing tools, Python tooling), those are
   the origin environment's REFERENCE implementations (Windows · PowerShell 5.1 · Git Bash).
   Satisfy the stated invariants with your platform's own idioms and conventions, and record the
   binding in ADR-0002 alongside the substrate mapping. The invariants are law; the commands are not.
4. Prove the setup with §7 (the setup self-test). **The Plane is not "set up" until the seeded
   violations FAIL the gate and the clean state PASSES it.** A gate that has never failed in test
   is presumed broken.
5. Everything in this spec is project policy unless your environment truly cannot provide it—in
   which case
   record the deviation as your project's ADR-0002 (ADR-0001 is the adoption itself), with the
   compensating control.

## §1 The idea

Software-project governance fails one way above all others: **FALSE-GREEN** — gates that pass
because they are self-attested, textual instead of behavioral, run against the repo instead of the
deployed artifact, or quietly weakened. Every mechanism in the Plane exists to make false claims
*unsatisfiable* rather than discouraged:

1. **The hub (task ledger) is THE source of truth** for all trackable state — tasks, decisions
   (ADRs), gaps, features, deploys, capabilities, notes. Anything that contradicts it is wrong
   until the ledger is amended.
2. **The ledger is LIVE.** Transitions are recorded at the moment of the event — a claim, a
   decision, a verification, a deploy — never batched, never reconstructed. In multi-agent
   campaigns the LEADER carries this duty personally and perfectionistically.
3. **The verifier identity must differ from the builder identity.** The leader verifies the
   worker; an independent verifier checks the product; the gate re-derives the verifier. Nobody
   stamps their own work.
4. **Gates are fail-closed and re-derive rather than trust.** A green flag contradicted by its
   underlying rows is FABRICATED-GREEN and blocks.
5. **Instance → Invariant.** Every defect is classified into a failure-mode taxonomy first; the
   fix is a class-wide detector with a self-test, never a point patch.
6. **Append-only history.** Decisions, registers, ledgers, and channels are appended and
   superseded, never rewritten; voided artifacts leave a tamper-evident trail.

The full statement of the laws is the `DOCTRINE.md` template in §4 — it ships in-context to every
agent on every project.

## §2 Architecture

### 2.1 Source-of-truth partition

| Store | Canonical for | Never |
|---|---|---|
| **Hub ledger** (event-sourced entities) | tasks, ADR status/links, gaps, features, deploys, capabilities, notes | hand-edited projections; batched transitions |
| **`PROJECT/` markdown** | prose of record: doctrine, charter, handoff, ADR full text, research, registers, contracts | duplicating hub facts without a `RENDERED VIEW` header |
| **`PROJECT/pm/` channels** | coordination traffic between agent seats | holding doctrine/decisions that aren't crystallized to canon |

### 2.2 The three substrate roles

The Plane requires three capabilities from ANY environment. Bind them to whatever the environment
provides — the roles, not the tools, are the requirement:

| Role | Requirement | Example bindings |
|---|---|---|
| **R1 Tamper-evident append-only ledger** | every entity transition is an event; hash-chained; verifiable end-to-end | the reference ledger (§2.3) · a signed git log · an event store |
| **R2 Schema-validated entity store** | entities validate on WRITE against schemas whose conditional rules make false claims unsatisfiable (§3) | reference fold+validate (§2.3) · Jira/Linear with required-field rules · GitHub Issues + CI schema check |
| **R3 Fail-closed gate runner** | one command re-derives all invariants; exit≠0 blocks merge/ship; wired as a REQUIRED check, not advisory | `plane_audit` (§2.4) as CI required check / pre-receive hook / pipeline gate |

**Never rebindable:** verifier ≠ builder · fail-closed · re-derive-over-trust · append-only
history · one-canonical-store-per-fact-class. An environment that cannot provide these is not a
binding target; it's a gap.

### 2.3 Reference substrate (the shipped filesystem Hub)

The `hub-scaffold` repository implements this binding in `hub_core/` with the Django HTTP adapter in
`adapters/django/hub/`. A standalone adopter can reproduce the same roles with any runtime, but must
not claim the controls are active until its implementation passes §7.

**Ledger** — `PROJECT/.hub/events.jsonl`, one canonical-JSON event per line, append-only:

```json
{"seq": 12, "ts": "2026-07-02T18:00:00Z", "aggregate": "myproj:task:0007",
 "type": "task.updated", "payload": {"status": "in_progress"},
 "prev_hash": "<hash of event 11>", "hash": "<see below>"}
```

- `hash` = SHA-256 hex of `prev_hash + canonical(selected event fields)`, where canonical JSON uses
  sorted keys, no insignificant whitespace, and UTF-8. The selected field order is defined by
  `hub_core.store._HASH_FIELDS`; genesis `prev_hash` is the empty string.
- `verify_chain`: replay the file recomputing every hash and linking every `prev_hash`; any
  mismatch = tampering = CRITICAL.
- Entity events use typed names such as `task.created`, `task.updated`, `task.transitioned`, and
  `adr.upserted`; log-only events such as `decision.logged` remain in the stream without projecting
  an entity. Evolve state with later events, never by editing prior lines.
- Writes are validated BEFORE append (R2): fold the aggregate's current state, merge the payload,
  validate against the schema — reject on any violation, including the conditional rules.
- `PROJECT/.hub/events.db` is a rebuildable SQLite index and transactional gatekeeper for aggregate
  versions/idempotency. JSONL remains canonical.

**Entities** — projections, never a store: fold events per aggregate in `seq` order,
last-write-wins per field. Any materialized view (JSON snapshot, dashboard, markdown table)
carries a "generated — canonical: ledger" header and is regenerated, never edited.

**Claims (multi-agent)** — `PROJECT/.hub/claims/<entity-id>.json` =
`{"task": id, "agent": seat, "token": nonce, "claimed": epoch, "expires": epoch}`; a completion
without a live claim is rejected; expired claims auto-release.

### 2.4 The gate runner (`manage.py hubaudit` in the reference binding)

One command; runs everywhere (CI required check + pre-ship + on demand); **fail-closed** (an
internal error is a RED, never a skip). Invariants, in order:

1. **Schema validity** — every folded entity validates, including the false-claim rules (§3). HIGH.
2. **Referential integrity** — every idref resolves; no dangling references. HIGH.
3. **ADR contiguity** — ADR numbers gap-free from 1; every `superseded` has a successor. WARN.
4. **Chain verification** — `verify_chain` intact. CRITICAL.
5. **Build coherence** (once deploys exist) — last deploy's `sha` matches the checkout/build stamp;
   when a caller supplies an independently observed `served` SHA, compare that too. Unknown ⇒
   AMBER, mismatch ⇒ HIGH. The base audit does not actively probe the live URL.
6. **Behavioral adapters** (environment-specific, added over time) — the Django binding checks a
   focused set of settings defaults and explicit guards on mutation routes.
7. **Project-specific controls** — live-ledger parity, stale leases, live probes, verifier gates,
   backups, and alert delivery are valuable but are not generic `hubaudit` checks; wire and test them
   when the project's threat/failure model requires them.

An audit adapter that raises becomes a CRITICAL violation rather than a silent skip.

The structured audit uses `exit_code`: `0` PASS · `2` blocking (critical/high) · `3` warn-only
amber. The Django management command returns process exit `0` for PASS or amber, `2` for blocking,
and `1` for an internal error (treat as RED). The write path adds its own guards: `done` cannot be minted directly. Completion
always requires a live claim, acceptance note, evidence, and a sound critical audit. In the default
`tracked` mode a verification command is optional; when present, the Hub requires the worker's
matching typed exit-0 receipt. `strict` additionally requires a command and dereferenceable
evidence. The Hub never executes task commands; the write token grants terminal board authority.

## §3 The entity model

Seven entity types. The conditional (`if/then`) rules are the heart: **they make false claims
unsatisfiable at write time.** Field lists are authoritative in the embedded schemas (§3.1).

| Type | Required | Status enum | Unsatisfiability rules |
|---|---|---|---|
| `task` | id, type, title, status, version | `todo · in_progress · blocked · done · dropped · shadow` (shadow = wired-but-inert, NOT done) | `done` ⇒ substantive `verified_by` ≥1 + `evidence_uri` ≥1 · `blocked` ⇒ `deps` ≥1 |
| `adr` | id, type, number, title, status, version | `proposed · accepted · superseded · deprecated · rejected` | accepted/superseded/deprecated ⇒ full `context_md`+`decision_md`+`consequences_md` · `superseded` ⇒ `superseded_by` ≥1 |
| `gap` | id, type, title, status, version | `open · investigating · mitigated · closed · wont-fix` | mitigated/closed ⇒ `addressed_by` ≥1 |
| `feat` | id, type, name, status, version | `shipped · partial · planned · experimental · removed` | shipped/partial ⇒ `tasks` ≥1 (no orphan feature claims) |
| `deploy` | id, type, sha, at, version | — (append-only record, written BY the act of deploying) | schema has no conditional proof rule; the project-specific deploy writer must derive `audit_ok` and `served_sha`, never accept them as self-attested input |
| `cap` | id, type, name, maturity, version | maturity: `concept · prototype · proven · reusable · extracted` | — (reuse fabric node) |
| `note` | id, type, title, version | `standing · superseded` | categories: discovery/gotcha/data/method/source/risk/context — a note is NOT a decision |

Ids are project-prefixed and type-segmented (`<project>:<type>:<local>`, regex in
`common.schema.json`), allocated once, never reused. The `hub:` `$id` prefix and the id regex's
project key are renameable bindings; the rules are not.

### 3.1 The schemas (verbatim)

<!-- TPL:PROJECT/schema/common.schema.json -->
````json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "hub:common",
  "title": "Hub shared definitions",
  "description": "Hub shared definitions. IDs are project-prefixed, type-segmented, allocated once, and never reused or renumbered.",
  "$defs": {
    "id": {
      "type": "string",
      "pattern": "^[a-z0-9][a-z0-9-]*:(task|adr|feat|gap|cap|deploy|note):[a-z0-9][a-z0-9._-]*$",
      "description": "Stable opaque id, e.g. {{PROJECT_KEY}}:task:0001, {{PROJECT_KEY}}:cap:sync.offline-cache"
    },
    "idref": {
      "type": "string",
      "pattern": "^[a-z0-9][a-z0-9-]*:(task|adr|feat|gap|cap|deploy|note):[a-z0-9][a-z0-9._-]*$",
      "description": "A machine-resolvable reference to another entity by id. The audit FAILS on any dangling idref."
    },
    "isoDate": { "type": "string", "format": "date-time" },
    "provenance": {
      "type": "object",
      "additionalProperties": false,
      "description": "Who/when/which-commit produced or last changed this entity.",
      "properties": {
        "created_at": { "$ref": "hub:common#/$defs/isoDate" },
        "updated_at": { "$ref": "hub:common#/$defs/isoDate" },
        "commits": { "type": "array", "items": { "type": "string" }, "description": "Implementing commit SHA(s)." },
        "author": { "type": "string" },
        "agent": { "type": "string", "description": "Agent id that last mutated this entity." },
        "model_version": { "type": "string" },
        "session_id": { "type": "string" }
      },
      "required": ["created_at"]
    },
    "evidenceUri": {
      "type": "string",
      "minLength": 1,
      "pattern": ".*\\S.*",
      "description": "A pointer to the real result: a commit, live operation, artifact, task receipt, or an explicitly invoked structural audit."
    }
  }
}
````
<!-- /TPL -->

<!-- TPL:PROJECT/schema/task.schema.json -->
````json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "hub:task",
  "title": "Task",
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "id": { "$ref": "hub:common#/$defs/id" },
    "type": { "const": "task" },
    "title": { "type": "string", "minLength": 1 },
    "status": { "enum": ["todo", "in_progress", "blocked", "done", "dropped", "shadow"], "description": "shadow = wired-but-inert, NOT done." },
    "priority": { "enum": ["P0", "P1", "P2", "P3"] },
    "phase": { "type": "string" },
    "acceptance": { "type": "string", "description": "Definition of done for this task." },
    "work_kind": {
      "enum": ["product", "content", "corpus", "governance", "verification", "decision", "research", "migration", "duplicate", "legacy"],
      "description": "The kind of work represented, so records that are not executable work stay queryable without masquerading as it. The A2A agent card publishes one skill per kind straight from this enum."
    },
    "verification_command": { "type": "string", "minLength": 1, "pattern": ".*\\S.*", "description": "Optional one-shot command for a rare critical-risk boundary. The worker runs it OUT-OF-BAND, retains the receipt, and removes every temporary test artifact before completion. Ordinary and copy/style work must omit it." },
    "verification_run": {
      "type": "array",
      "description": "Historical receipts for an optional one-shot critical-boundary command run OUT-OF-BAND. The receipt persists and composes upward; temporary test artifacts do not.",
      "items": {
        "type": "object", "additionalProperties": false,
        "properties": {
          "command": { "type": "string", "minLength": 1 },
          "exit_code": { "type": "integer" },
          "output_sha256": { "type": "string", "pattern": "^[0-9a-f]{64}$" },
          "ran_by": { "type": "string", "minLength": 1 },
          "ran_at": { "type": "string" }
        },
        "required": ["command", "exit_code"]
      }
    },
    "touches": { "type": "array", "items": { "type": "string" }, "description": "Files/areas this task changes." },
    "plan": {
      "type": "array",
      "items": { "type": "object", "additionalProperties": false, "properties": { "step": { "type": "string" }, "done": { "type": "boolean" } }, "required": ["step", "done"] },
      "description": "Persisted, resumable checklist."
    },
    "not_before": { "type": "string", "description": "Durable timer: an ISO-8601 instant before which this task is not offered to a worker. It is WAITING, not blocked and not drained — the readiness rail reports snoozed work separately so a deferred task never reads as an empty board." },
    "poison_blocked": { "type": "boolean", "description": "The circuit breaker opened after repeated failing receipts; the task is withheld from the queue until an exit-0 receipt clears it, so a broken task cannot consume the whole fleet in a retry storm." },
    "poison_reason": { "type": "string", "description": "Why the circuit breaker opened, surfaced verbatim on the attention rail." },
    "deps": { "type": "array", "items": { "$ref": "hub:common#/$defs/idref" }, "description": "Blocked iff any dep is not done." },
    "implements": { "type": "array", "items": { "$ref": "hub:common#/$defs/idref" }, "description": "feat/cap this realizes." },
    "decided_by": { "type": "array", "items": { "$ref": "hub:common#/$defs/idref" }, "description": "ADR(s) governing this task." },
    "verified_by": { "type": "array", "items": { "type": "string", "minLength": 1, "pattern": ".*\\S.*" }, "description": "Substantive result summaries from the completed real operation; >=1 required for done." },
    "evidence_uri": { "type": "array", "items": { "$ref": "hub:common#/$defs/evidenceUri" } },
    "surfaced_by": { "$ref": "hub:common#/$defs/idref", "description": "The task/work during which this was scouted." },
    "source": { "type": "string", "description": "Where it came from, e.g. REVIEW-G3, CHARTER, RESEARCH-HISTORY." },
    "legacy_ref": { "type": "string", "description": "Pre-migration id, e.g. #V4.7 / A3." },
    "version": { "type": "integer", "minimum": 0, "description": "Per-aggregate OCC version." },
    "provenance": { "$ref": "hub:common#/$defs/provenance" }
  },
  "required": ["id", "type", "title", "status", "version"],
  "allOf": [
    { "if": { "properties": { "status": { "const": "done" } }, "required": ["status"] }, "then": { "properties": { "verified_by": { "type": "array", "minItems": 1 }, "evidence_uri": { "type": "array", "minItems": 1 } }, "required": ["verified_by", "evidence_uri"] } },
    { "if": { "properties": { "status": { "const": "blocked" } }, "required": ["status"] }, "then": { "properties": { "deps": { "type": "array", "minItems": 1 } }, "required": ["deps"] } },
    { "if": { "properties": { "work_kind": { "enum": ["product", "verification"] } }, "required": ["work_kind"] }, "then": { "properties": { "acceptance": { "minLength": 1 } }, "required": ["acceptance"] } },
    { "if": { "properties": { "work_kind": { "const": "decision" } }, "required": ["work_kind"] }, "then": { "properties": { "decided_by": { "type": "array", "minItems": 1 } }, "required": ["decided_by"] } },
    { "if": { "properties": { "work_kind": { "const": "research" } }, "required": ["work_kind"] }, "then": { "properties": { "evidence_uri": { "type": "array", "minItems": 1 } }, "required": ["evidence_uri"] } }
  ]
}
````
<!-- /TPL -->

<!-- TPL:PROJECT/schema/adr.schema.json -->
````json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "hub:adr",
  "title": "Architecture Decision Record",
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "id": { "$ref": "hub:common#/$defs/id" },
    "type": { "const": "adr" },
    "number": { "type": "integer", "minimum": 1, "description": "Gap-free sequential ADR number." },
    "title": { "type": "string", "minLength": 1 },
    "status": { "enum": ["proposed", "accepted", "superseded", "deprecated", "rejected"] },
    "context_md": { "type": "string", "minLength": 1, "pattern": ".*\\S.*", "description": "Substantive context; immutable post-accept." },
    "decision_md": { "type": "string", "minLength": 1, "pattern": ".*\\S.*", "description": "Substantive decision; immutable post-accept." },
    "consequences_md": { "type": "string", "minLength": 1, "pattern": ".*\\S.*" },
    "amendments_md": { "type": "string", "description": "Dated, append-only amendments (the legal way to evolve an Accepted ADR short of supersession)." },
    "supersedes": { "type": "array", "items": { "$ref": "hub:common#/$defs/idref" } },
    "superseded_by": { "type": "array", "items": { "$ref": "hub:common#/$defs/idref" } },
    "legacy_ref": { "type": "string" },
    "version": { "type": "integer", "minimum": 0 },
    "provenance": { "$ref": "hub:common#/$defs/provenance" }
  },
  "required": ["id", "type", "number", "title", "status", "version"],
  "allOf": [
    { "if": { "properties": { "status": { "enum": ["accepted", "superseded", "deprecated"] } }, "required": ["status"] }, "then": { "required": ["context_md", "decision_md", "consequences_md"] } },
    { "if": { "properties": { "status": { "const": "superseded" } }, "required": ["status"] }, "then": { "properties": { "superseded_by": { "type": "array", "minItems": 1 } }, "required": ["superseded_by"] } }
  ]
}
````
<!-- /TPL -->

<!-- TPL:PROJECT/schema/gap.schema.json -->
````json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "hub:gap",
  "title": "Gap / finding",
  "description": "A reviewed, evidence-backed finding that represents a real project gap.",
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "id": { "$ref": "hub:common#/$defs/id" },
    "type": { "const": "gap" },
    "title": { "type": "string", "minLength": 1 },
    "status": { "enum": ["open", "investigating", "mitigated", "closed", "wont-fix"] },
    "severity": { "enum": ["P0", "P1", "P2", "P3"] },
    "evidence": { "type": "string", "description": "file:line / observed behavior backing the finding." },
    "source": { "type": "string", "description": "REVIEW-G3 / CHARTER-security / RESEARCH-HISTORY-4.5 / LEDGER-Needs." },
    "addressed_by": { "type": "array", "items": { "$ref": "hub:common#/$defs/idref" }, "description": "Task(s) that close this gap." },
    "legacy_ref": { "type": "string" },
    "version": { "type": "integer", "minimum": 0 },
    "provenance": { "$ref": "hub:common#/$defs/provenance" }
  },
  "required": ["id", "type", "title", "status", "version"],
  "allOf": [
    { "if": { "properties": { "status": { "enum": ["mitigated", "closed"] } }, "required": ["status"] }, "then": { "properties": { "addressed_by": { "type": "array", "minItems": 1 } }, "required": ["addressed_by"] } }
  ]
}
````
<!-- /TPL -->

<!-- TPL:PROJECT/schema/feat.schema.json -->
````json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "hub:feat",
  "title": "Feature",
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "id": { "$ref": "hub:common#/$defs/id" },
    "type": { "const": "feat" },
    "name": { "type": "string", "minLength": 1 },
    "status": { "enum": ["shipped", "partial", "planned", "experimental", "removed"] },
    "summary": { "type": "string" },
    "tasks": { "type": "array", "items": { "$ref": "hub:common#/$defs/idref" }, "description": "Implementing tasks. Required for shipped/partial (no orphan feature claims)." },
    "adrs": { "type": "array", "items": { "$ref": "hub:common#/$defs/idref" } },
    "capability": { "$ref": "hub:common#/$defs/idref", "description": "The capability this feature realizes." },
    "evidence_uri": { "type": "array", "items": { "$ref": "hub:common#/$defs/evidenceUri" } },
    "legacy_ref": { "type": "string" },
    "version": { "type": "integer", "minimum": 0 },
    "provenance": { "$ref": "hub:common#/$defs/provenance" }
  },
  "required": ["id", "type", "name", "status", "version"],
  "allOf": [
    { "if": { "properties": { "status": { "enum": ["shipped", "partial"] } }, "required": ["status"] }, "then": { "properties": { "tasks": { "type": "array", "minItems": 1 } }, "required": ["tasks"] } }
  ]
}
````
<!-- /TPL -->

<!-- TPL:PROJECT/schema/deploy.schema.json -->
````json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "hub:deploy",
  "title": "Deploy record (append-only; written BY the act of deploying, never by hand)",
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "id": { "$ref": "hub:common#/$defs/id" },
    "type": { "const": "deploy" },
    "build": { "type": "string" },
    "sha": { "type": "string", "description": "The git SHA this deploy record claims was shipped." },
    "method": { "type": "string" },
    "at": { "$ref": "hub:common#/$defs/isoDate" },
    "audit_ok": { "type": "boolean", "description": "Must be derived by the trusted deploy writer from the gate exit; the schema alone cannot prove it." },
    "served_sha": { "type": ["string", "null"], "description": "What the live artifact reported back (coherence proof); null until a canary confirms." },
    "tasks_closed": { "type": "array", "items": { "$ref": "hub:common#/$defs/idref" } },
    "legacy_ref": { "type": "string" },
    "version": { "type": "integer", "minimum": 0 },
    "provenance": { "$ref": "hub:common#/$defs/provenance" }
  },
  "required": ["id", "type", "sha", "at", "version"]
}
````
<!-- /TPL -->

<!-- TPL:PROJECT/schema/cap.schema.json -->
````json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "hub:cap",
  "title": "Capability",
  "description": "A reusable system or architecture node in this project's capability graph. Cross-project discovery requires an external registry or federation binding.",
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "id": { "$ref": "hub:common#/$defs/id" },
    "type": { "const": "cap" },
    "name": { "type": "string", "minLength": 1 },
    "cap_version": { "type": "string", "description": "Published contract version." },
    "iface": { "type": "string", "description": "REST/MCP/js-module/rust-crate/python-module/doc-playbook signature." },
    "kind": { "enum": ["http_verb", "js_module", "rust_crate", "python_module", "doc_playbook", "service"] },
    "maturity": { "enum": ["concept", "prototype", "proven", "reusable", "extracted"] },
    "pivot_notes": { "type": "string", "description": "How to reference/pivot this into a future project." },
    "needs": { "type": "string", "description": "What it still needs (doubles as cross-project backlog)." },
    "realized_by": { "type": "array", "items": { "$ref": "hub:common#/$defs/idref" }, "description": "feat/task that realize it." },
    "depends_on": { "type": "array", "items": { "$ref": "hub:common#/$defs/idref" } },
    "consumed_by": { "type": "array", "items": { "$ref": "hub:common#/$defs/idref" } },
    "commercial_ok": { "type": "boolean" },
    "health_endpoint": { "type": "string" },
    "legacy_ref": { "type": "string" },
    "version": { "type": "integer", "minimum": 0 },
    "provenance": { "$ref": "hub:common#/$defs/provenance" }
  },
  "required": ["id", "type", "name", "maturity", "version"]
}
````
<!-- /TPL -->

<!-- TPL:PROJECT/schema/note.schema.json -->
````json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "hub:note",
  "title": "Finding / Note",
  "description": "Context discovered along the way — facts, gotchas, data realities, method limits — so a later agent doesn't have to rediscover it. NOT a decision (that's an ADR).",
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "id": { "$ref": "hub:common#/$defs/id" },
    "type": { "const": "note" },
    "title": { "type": "string", "minLength": 1 },
    "category": { "enum": ["discovery", "gotcha", "data", "method", "source", "risk", "context"],
                  "description": "discovery=fact learned · gotcha=trap · data=dataset reality · method=how-to + its limits · source=a source's shape · risk=a way this can mislead/fail · context=general." },
    "body_md": { "type": "string", "description": "The finding in full, incl. limits/caveats." },
    "status": { "enum": ["standing", "superseded"], "description": "standing = still true; superseded = replaced by a newer note." },
    "tags": { "type": "array", "items": { "type": "string" } },
    "relates_to": { "type": "array", "items": { "$ref": "hub:common#/$defs/idref" }, "description": "task/adr/feat this came from or informs." },
    "found_at": { "type": "string", "description": "when/where it was learned." },
    "version": { "type": "integer", "minimum": 0 },
    "provenance": { "$ref": "hub:common#/$defs/provenance" }
  },
  "required": ["id", "type", "title", "version"]
}
````
<!-- /TPL -->

### 3.2 Portable project identity

`init.sh` emits this file with every placeholder substituted. It is the shared identity for the
Hub core, MCP server discovery, the root agent discovery card, receipt predicates, and the local
worker scheme.

<!-- TPL:PROJECT/project.json -->
````json
{
  "key": "{{PROJECT_KEY}}",
  "brand": "{{BRAND}}",
  "app_name": "{{PROJECT_KEY}}",
  "app_host": "{{LIVE_URL}}",
  "worker_scheme": "hub-{{PROJECT_KEY}}"
}
````
<!-- /TPL -->

### 3.3 Genesis seed (example shape)

<!-- TPL:PROJECT/seed.json -->
````json
{
  "adrs": [
    {"ref":"template-1","number":1,"status":"accepted",
     "title":"A new app is a clean scaffold instance whose ROOT serves the app",
     "context_md":"Cloning-and-pivoting an existing project drags the donor's landing page, branding, and cruft onto the new root (this pattern has shipped apps with the wrong front door).",
     "decision_md":"Start every app from the scaffold via init.sh: root serves the app, hub at /hub, born-safe, born-governed.",
     "consequences_md":"The deployed root URL always presents the app; no donor cruft; every project starts governed."},
    {"ref":"template-2","number":2,"status":"accepted",
     "title":"PROJECT/ is the Project Plane: the canonical content-agnostic management framework",
     "context_md":"Projects accumulated decisions, research, gaps, verification, and audit history in ad-hoc files; multi-agent campaigns improvised their own protocol, registers, and gates mid-flight, and governance lagged the work layer.",
     "decision_md":"Every project is born with the Project Plane (PROJECT/README.md = the spec): hub ledger canonical for trackable entities; DOCTRINE/CHARTER/HANDOFF spine; ADR + research + registers + audit + verify + runs + worklogs + ops; pm/PROTOCOL.md as the leader/worker/verifier campaign law.",
     "consequences_md":"One source of truth per fact class; channels crystallize into canon same-session; ships gate on re-derived green; any cold agent resumes from HANDOFF.md alone."}
  ],
  "tasks": [
    {"ref":"T1","phase":"0 Genesis","status":"todo","priority":"P1","decided_by":["template-1"],
     "title":"Build the app: mount the hub per adapters/django/MOUNTING.md, implement the first feature, then deploy via your deploy contract"},
    {"ref":"T2","phase":"0 Genesis","status":"todo","priority":"P1","decided_by":["template-2"],
     "title":"Adopt the Project Plane: fill CHARTER.md + HANDOFF.md, read DOCTRINE.md, seed TRUTH-MATRIX for your first surface"}
  ],
  "notes": []
}
````
<!-- /TPL -->

## §4 The PROJECT/ folder — manifest + every template, verbatim

Instantiate exactly this tree (plus `.plane/` or your R1/R2 binding). Every file below opens with
a role header (`canonical | view | channel | template`); fill-ins are `<...>`.

```
PROJECT/
  README.md  CHARTER.md  DOCTRINE.md  HANDOFF.md  project.json  seed.json  schema/(8 files)
  ADR/README.md + 0000-template.md
  research/README.md + RESEARCH-HISTORY.md
  registers/FAILURE-MODES.md INCIDENTS.md TRUTH-MATRIX.md BLINDSPOTS.md DECISIONS-PENDING.md GLOSSARY.md
  audit/README.md
  verify/README.md + MANIFEST-CONTRACT.md
  runs/README.md    worklogs/README.md    ops/INFRA-INVENTORY.md
  pm/PROTOCOL.md + seats/{LEADER,WORKER-1,VERIFIER}/CHARTER.md
```

### 4.1 `PROJECT/README.md` — the framework spec & map
<!-- TPL:PROJECT/README.md -->
````markdown
# THE PROJECT PLANE — canonical PROJECT/ framework (v1.1, 2026-08-03)

> canonical · owner: whoever leads the project · update: only by ADR (this file is the framework spec)

`HUB-QUALITY.md` is the canonical construction contract for every new or materially upgraded Hub.

Every app owns its code; **this folder owns everything about how the project is run**: decisions,
research, doctrine, gaps, completion receipts, incident routing, and agent coordination. It is **content-agnostic**
— nothing in the framework refers to any particular app. It was crystallized from live-fire
multi-agent campaigns, the hub platform, and a hard-won doctrine corpus. Your organization's global
doctrine documents (lifecycle framework · method playbook · quality charter) are home-ecosystem
bindings, replaceable per §7. Where any older doc lists legacy PROJECT/ file sets
(TASKS/FEATURES/CHANGELOG/DEPLOYS markdown), **this manifest supersedes them**: those facts live in
the hub ledger now.

## Contract status: normative versus active

This tree is a portable framework and set of templates. The reference Hub makes the entity ledger,
schemas, projections, and on-demand `hubaudit` available after it is mounted. Empty
directories/contracts such as `verify/`, `runs/`, `worklogs/`, `audit/`, and the multi-seat `pm/`
topology describe optional roles and durable records; they do not require standing verifiers,
tests, fixtures, scheduled checks, monitors, or CI workflows. Record only controls that are
actually active in `HANDOFF.md` and the Hub.

## Throughput-first completion law

- The successful real operation is the default proof. Perform it, record the observed outcome,
  mark the task done, and stop.
- Copy, wording, style, spacing, color, animation polish, and all other non-critical changes get no
  test or automated page-copy validation.
- Permanent tests, fixtures, checker scripts, and standing verification workflows are prohibited.
  A rare security, destructive-data, migration, protocol, or concurrency boundary may use one
  temporary probe outside the durable product tree; run it once, retain its receipt, and delete it
  before commit.
- Completed receipts compose. Parent tasks and releases inherit child receipts and inspect only a
  new critical integration seam; they never replay accepted proof or create verifier fan-out.
- A real failure becomes a fresh Hub repair task and may route to a dedicated error-fixing agent,
  preserving delivery-agent focus and queue throughput.

## 0. Read-order for a cold agent

1. **`HANDOFF.md`** — you-are-here: current state, in-flight work, quirks. Always first.
2. **`CHARTER.md`** — what this project is, its quality bar, its definition of done.
3. **`DOCTRINE.md`** — the standing laws you must not violate.
4. **The hub** — `/hub` (or fold `PROJECT/.hub/events.jsonl`) for canonical
   tasks/ADRs/gaps/features/deploys. Use `hubaudit` only for an explicitly scoped ledger-integrity
   operation, not as a routine task-completion gate.
5. **`pm/PROTOCOL.md`** — only if a multi-agent campaign is active (HANDOFF says so).

## 1. The manifest

| Path | Artifact class | Canonical? |
|---|---|---|
| `README.md` | the framework spec + map | canonical (framework) |
| `CHARTER.md` | mission · scope · quality bar · definition of done | canonical |
| `DOCTRINE.md` | standing laws (operator contract + crystallized project laws) | canonical |
| `HANDOFF.md` | living continuity file — the single resume entry point | canonical, always current |
| `project.json` | portable identity: key · brand · app name/host · worker scheme | canonical identity for Hub/MCP/discovery |
| `seed.json` · `schema/` · `.hub/` | reference-Hub genesis · entity schemas · runtime hash-chained event ledger | `.hub/events.jsonl` = the entity ledger after the reference Hub is activated |
| `ADR/` | numbered decision records (full prose of record) | canonical prose; hub `adr` entity canonical for status/links |
| `research/` | deep research: dossiers, MoE panels, improvement-surface memos + `RESEARCH-HISTORY.md` chronicle | canonical |
| `registers/` | what hub schemas don't model: failure-mode taxonomy, incidents, truth matrix, blind spots, pending operator decisions, glossary | canonical |
| `audit/` | filed point-in-time audit artifacts (MoE registers, audit runs, security reviews) | canonical artifacts |
| `verify/` | transient critical-boundary proof contract | canonical policy; no standing harness |
| `runs/` | optional durable receipts for completed real operations | canonical only for receipts actually filed |
| `worklogs/` | per-workstream execution logs with measured before/after | canonical |
| `ops/` | infra inventory / deploy runbook | canonical, date-stamped |
| `pm/` | multi-agent campaign kit: protocol, seats, channels | channels = operational log, NOT a governance store |

**Not in this folder:** tasks, gaps, features, deploys, capabilities — those are **hub entities**
(schema-constrained and hash-chained). Markdown renderings of hub data are views and must
say so (see §3).

## 2. Where the audit history lives (the user-visible answer to "what happened?")

- **`.hub/events.jsonl`** — the tamper-evident spine for transitions actually recorded through the
  reference Hub: every task/ADR/gap/deploy transition,
  SHA-256 hash-chained, append-only. `hubaudit` verifies the chain + schema + referential integrity
  + build coherence; critical/high states block, while explicitly unknowable pre-first-deploy
  coherence is reported amber.
- **Hub `deploy` entities** — the required record for each deploy once the project's deploy writer
  is wired. The base HTTP API has no deploy endpoint; the deploy integration must validate and
  append it, and must derive `audit_ok` rather than accept a human assertion.
- **`audit/`** — dated point-in-time audit artifacts (MoE finding registers, review verdicts).
- **Hub task evidence / `runs/`** — durable receipts for completed real operations and the rare
  critical one-shot probe. The temporary probe itself never lives here.
- **`registers/INCIDENTS.md`** — observed failures, their fresh repair tasks, routes, and successful retries.

## 3. Source-of-truth law

1. **The hub is THE source of truth for all trackable state** — tasks, ADRs, gaps, features,
   deploys, capabilities, notes. One canonical store per fact class: hub = entities; markdown =
   prose + the registers above; channels (`pm/`) = coordination traffic only. Anything that
   contradicts the hub is wrong until the hub is amended.
2. **Every prose artifact opens with a role header**: `> canonical | view (source: X) | channel | template`
   plus owner and update trigger. A view that could be mistaken for canon is a defect.
3. **Views declare and never lead.** A rendered table of hub data carries
   `RENDERED VIEW — canonical: hub` and is regenerated, never hand-drifted.
4. **The ledger is LIVE.** Entity transitions are recorded at the moment of the event (claim →
   `in_progress`, decision → ADR, real operation completed → `done`+evidence, deploy → deploy entity) — never
   batched, never reconstructed later. Doctrine or decisions born in pm traffic MUST be recorded
   (ADR + register + hub) before the traffic moves on — see `pm/PROTOCOL.md` §11; in campaigns
   the LEADER owns this personally.

## 4. ID namespaces

| Prefix | Meaning | Home |
|---|---|---|
| `ADR-NNNN` | decision record, gap-free ascending | `ADR/` + hub `adr` |
| task ids (`P<phase>-<n>` or slug) | hub tasks | hub |
| `L-`, `W<n>-`, `V-` + number | pm directives per seat (leader-issued) | `pm/seats/*/DIRECTIVES.md` |
| `OP-<n>` | operator-issued directive | any channel, marked `who: operator` |
| `FM-<grp><n>` | failure-mode class row | `registers/FAILURE-MODES.md` |
| `INC-NNN` | defect/incident instance | `registers/INCIDENTS.md` |
| `DP-NN` | pending operator decision | `registers/DECISIONS-PENDING.md` |
| `BS-NN` | blind-spot / missing signal | `registers/BLINDSPOTS.md` |
| receipt ids (`<UTCstamp>` / `<scope>-v<n>`) | optional durable operation/critical-boundary receipts | hub evidence · `runs/` |

New namespaces must be declared in `registers/GLOSSARY.md` before first use.

## 5. Amendment & supersession (one convention, everywhere)

- **Append-only artifacts are never rewritten.** To change one: add a dated
  `**Amendment (YYYY-MM-DD):**` block stating what changes and that it is authoritative over the
  text above — or supersede the whole artifact (new number/version + `superseded_by` both ways).
- **Numbering is repaired, never reused**: a collision or skip gets a `CORRECTION` entry; existing
  ids keep their meaning forever.
- **Voiding**: artifacts discovered to be wrong/fabricated are MOVED to an `_archive/voided-<ts>/`
  under their home dir plus a `void` event on the record stream — never silently deleted (the void
  trail is itself audit history).
- **Published identifiers are immutable** (seed titles, feed UIDs, ADR numbers, external URLs):
  changing one is a supersession event, never an edit.

## 6. Lifecycle

This folder is phase-agnostic; the lifecycle spine (CREATE → REFINE → DEPLOY → INTEGRATE →
MAINTAIN) and any organization-specific collaboration methods live in your organization's global
doctrine documents (see the header). `DOCTRINE.md` carries the throughput and critical-boundary
laws that must be in context at all times; everything else is subsumed by reference—do not
re-paste global doctrine here.

## 7. Portability — rebinding the Plane to any environment

The framework is a set of **roles**, not tools. It runs anywhere that can provide the two standing
substrate roles below and, only when required, the transient third role:

| Role the Plane requires | Home-ecosystem binding | Rebind to (examples) |
|---|---|---|
| **Tamper-evident append-only ledger** (entity transitions, hash-chained) | `hub_core` store → `.hub/events.jsonl` | any event store, signed git log, ledgered DB |
| **Schema-validated entity store with false-claim-unsatisfiable rules** (done⇒verified_by+evidence, etc.) | hub entities + `schema/*.json` + `seedhub` | Jira/Linear required-field rules, GitHub Issues + API constraints |
| **Transient critical-boundary proof** (optional, one-shot) | protected real operation or disposable probe + durable Hub receipt | an out-of-process actor performing the scoped operation and filing its receipt |

Rebinding rules:
1. **Every path outside this folder is a BINDING, not a dependency.** The global-doctrine docs in
   the header, the deploy atlas, and hub commands are the home ecosystem's instances; when
   pivoting, replace them with the target environment's equivalents and re-point the citations —
   the laws in `DOCTRINE.md`, the registers, and `pm/PROTOCOL.md` transfer verbatim.
2. **The protocol's channel mechanics are substrate-independent** (`pm/PROTOCOL.md` §13): append-only
   files + monitors are the proven floor; any addressable bus with per-seat ACLs may replace them
   by ADR without changing the event vocabulary or duties.
3. **What may never be rebound away:** actual-operation proof by default; no validation for
   copy/style/non-critical changes; no permanent test, fixture, or verification workflow;
   identity separation for a declared critical one-shot boundary; compositional receipts;
   append-only history; and one canonical store per fact class. An environment that cannot provide
   these is a gap.
````
<!-- /TPL -->

### 4.2 `PROJECT/CHARTER.md`
<!-- TPL:PROJECT/CHARTER.md -->
````markdown
# CHARTER — <project name>

> template → becomes canonical once filled · owner: leader · update: by ADR only (scope changes are decisions)

Any Hub named by this charter also satisfies `HUB-QUALITY.md`; beauty, realtime truth, and task
throughput are completion criteria, not optional polish.

The charter is the contract for WHAT this project is and WHEN it is done. It changes rarely and
only by ADR. If work in flight contradicts the charter, the work is wrong or the charter needs an
ADR — never both silently.

## 1. Mission
One paragraph: what this delivers, for whom, and the single sentence a stranger repeats back.

## 2. Users & surfaces
Who touches it (operator, public, agents) and through what (web root, API, feeds, /hub).

## 3. Scope
The capabilities this project promises. Each maps to hub `feat` entities once building starts.

## 4. Non-goals
Explicitly out of scope, with the reason. A non-goal may only move into scope via ADR.

## 5. Quality bar
- **Born-safe:** prod settings hardened at birth (no DEBUG default, no committed secrets,
  unauthenticated reads only when contents are publishable, general writes token-gated).
- **Truth-first:** every rendered assertion derives from gathered evidence (`DOCTRINE.md` §2) —
  the truth matrix (`registers/TRUTH-MATRIX.md`) is the acceptance checklist for any new surface.
- **Gate-green:** `hubaudit` PASS + every project invariant gate that has actually been implemented
  and wired is a ship precondition, fail-closed. A contract file without a runner is not green.
- **Best-of-breed:** research before build (`research/README.md`); re-architect rather than polish a failing approach.

## 6. Definition of done (project-level)
Machine-binary first, judgment second:
1. All promised capabilities exist, are WIRED (reachable in the served artifact), and are hub `feat: shipped` with linked tasks.
2. `hubaudit` exit 0; invariant gates green; deploy coherence proven (served SHA == blessed SHA).
3. Live front-door verification passed (patient canary, real UA, asserts the app's own body).
4. Zero open P0/P1 gaps; every open `DP-` decision either resolved or explicitly deferred by the operator.
5. `HANDOFF.md` current; audit history complete (every deploy/run/incident recorded).

## 7. Run model & cost ceiling
Compute tiers this project may use, hard cost ceiling, and what happens at the ceiling (stop, not degrade silently).

## 8. Data & legal posture
Where data comes from, what may be stored/published, rate-limit/courtesy rules, takedown stance.
````
<!-- /TPL -->

### 4.3 `PROJECT/DOCTRINE.md` — the standing laws
<!-- TPL:PROJECT/DOCTRINE.md -->
````markdown
# DOCTRINE — standing laws

> canonical · owner: leader · update: append §6 laws as they are crystallized (each cites its ADR); §§1–5 change only with the framework

The Hub product itself is governed by `HUB-QUALITY.md`: visual hierarchy, motion meaning,
accessibility, realtime freshness, and throughput truth are one quality bar.

These are the laws every agent on this project operates under, regardless of content. They are the
distillation of every hard lesson to date. Violating one is a defect even when the output "works".
They are normative policy; `README.md` identifies which reference-Hub controls are shipped and which
require project-specific wiring.

## §1 Operator contract
1. **Zero decisions pushed to the operator.** Best-guess every fork, record it (ADR if architectural,
   `DP-` entry if genuinely operator-only), and proceed. Asking permission to continue is a defect.
2. **Drive to done.** Once a goal is set, execute to completion. Pause only for: an irreversible or
   destructive act, a privileged/undefined-secret operation, or a true operator-only decision —
   and even then, queue it in `registers/DECISIONS-PENDING.md` and route around it.
3. **No device-test gates.** Never frame a milestone as "waiting on the operator to test".
   Implement full scope; when a feature inherently requires a device, use it through the real
   operation and record the outcome.
4. **Best way, no thrashing.** Research best-of-breed first; a named technology is a hypothesis,
   not a mandate; when an approach keeps failing, re-architect — don't polish.
5. **Finish first; prove only what is at risk.** The successful real operation is the default proof.
   Copy, wording, style, animation polish, and other non-critical changes get no test or validation
   ritual. Permanent tests, fixtures, verifier scripts, and always-on verification workflows are
   forbidden. A security, destructive-data, migration, protocol, or concurrency boundary may earn
   one temporary probe: create it outside the durable product tree, run it once, retain its receipt,
   and delete it before commit. Completed receipts compose upward; a release checks only a newly
   created critical integration seam and never reruns accepted child work.
6. **Track and document, always.** Every directed change gets a hub task AND a decision record.
   Note every downstream artifact a shared-state change invalidates.

## §2 Truth discipline (anti-false-green)
1. **FALSE-GREEN is the meta-failure at a declared critical boundary.** A boundary receipt must
   describe what actually ran and what happened. When independent proof is explicitly warranted,
   **the verifier identity must differ from the builder identity**. Ordinary work creates no gate,
   standing verifier, or scheduled proof burden.
2. **ASSERTED ≠ DERIVED = BROKEN.** Machine-derived factual claims such as status, ordering, and
   counts must trace to their source of truth. Editorial copy, visual style, and motion are not
   verification targets. `registers/TRUTH-MATRIX.md` records only factual derivations that matter.
3. **Done ≠ live.** A task whose value requires a deploy is NOT done until the deploy-owner is
   notified (a `deploy_request` event naming code/data + SHA) and the real deploy outcome is
   observed live.
4. **Evidence is the completed operation.** Record the attempted action and observed result after
   the final edit. If a rare critical probe is used, its durable receipt must postdate the edit;
   the probe itself must not survive the commit.
5. **Receipts compose.** Consumers inherit accepted dependency receipts. They do not replay them;
   a release examines only a new critical integration seam introduced by composition.
6. **Stop when the changed behavior works.** Once the real operation succeeds and no critical
   boundary remains unobserved, completion is earned. Adding another check is process bloat.

## §3 Defect discipline (Instance → Invariant)
1. **Observed failure becomes work.** Record the concrete failure as an `INC-` instance and open a
   fresh repair task; classify it in `registers/FAILURE-MODES.md` when the class is useful for routing.
2. **Restore the real operation.** Fix the causal path and retry the action that failed. The
   successful retry is the ordinary completion receipt.
3. **Do not bank tests.** A failure does not automatically create a regression suite, fixture, or
   permanent checker. A rare critical recurring boundary may use a one-shot temporary diagnostic
   probe under §1.5, deleted before commit.
4. **Repair can be its own lane.** Projects may route observed failures to a dedicated error-fixing
   agent so delivery agents keep completing planned work; the Hub keeps both lanes visible.
5. **Stop after recovery.** Once the failed operation succeeds, close the repair task and return
   throughput to the delivery queue.

## §4 Change discipline
1. **Research precedes build.** No architectural work starts before its research is captured in
   `research/` — the RESEARCH-HISTORY chronicle is the front door to "why".
2. **Decisions are ADRs** — append-only, gap-free, rejected-alternatives on record, supersede-never-rewrite.
3. **Registers are append-only**; amendments follow `README.md` §5. Published identifiers are immutable.
4. **The ledger is LIVE:** the hub is updated AT THE MOMENT of the event — task claimed →
   `in_progress`; decision made → ADR recorded; real operation completed → `done` with `verified_by`;
   deploy finished → deploy entity. Transitions are never batched or reconstructed afterwards;
   same-session is the outer bound for prose docs only. A governance layer that lags the work
   layer is itself a defect (a real campaign once created 221 tasks and transitioned 14 — the
   board was fiction). In campaigns the LEADER carries this duty personally (PROTOCOL §11).
5. **Shared-kit changes** (anything vendored across projects) get a CHANGELOG entry in the kit.
6. **Contracts never impersonate controls.** A documented gate, verifier, backup, canary, scanner,
   or alert is reported as active only while its real critical boundary, trigger, and owner exist.
   Documentation never creates a standing test obligation.

## §5 Autonomy discipline
1. **Two attempts, then escalate** with what you tried. Timebox unfamiliar rabbit holes (~20 min).
2. **Question-then-move-on:** post the question, keep working everything not blocked by it.
3. **Anti-stall:** cap per-item effort in bulk sweeps; close as INSUFFICIENT and continue rather than spiral.
4. **No filler traffic:** no "ready to X" posts, no permission-seeking, no context/compaction
   narration — continuity lives in `HANDOFF.md`/seat `STATE.md`, not in worry.

## §6 Project laws (append below; each cites its ADR)
<!-- Crystallized, project-specific laws land here as they are born. Format:
N. **<law>** (ADR-NNNN, YYYY-MM-DD): <one-paragraph statement>. -->
````
<!-- /TPL -->

### 4.4 `PROJECT/HANDOFF.md`
<!-- TPL:PROJECT/HANDOFF.md -->
````markdown
# HANDOFF — living continuity file

> canonical · owner: the principal agent (solo) or LEADER (campaign) · update: at every significant state change and ALWAYS before ending a session

This is the single resume entry point. A cold agent reads this first (`README.md` §0) and must be
able to continue seamlessly from it alone plus the tails of any active channels. Keep it current —
a stale handoff is a defect. Rewrite in place (it is a snapshot, not a ledger); history lives in
the hub and the channels.

## 0. The arrangement
Operating mode (SOLO / campaign per `pm/PROTOCOL.md` §0 with seat roster), who deploys what, and
where the channels are. If a campaign is active: "read `pm/PROTOCOL.md`, then your seat's CHARTER,
DIRECTIVES tail, and STATE."

## 1. Standing doctrine deltas
Nothing here duplicates `DOCTRINE.md` — list only recent laws not yet internalized by all seats,
with their ADR numbers.

## 2. What's live right now (all verified, with evidence)
- Deployed code SHA + how it was verified.
- Deployed data state + gate status (`runs/status.json` when the project has implemented that
  contract; otherwise name the real gate artifact and the missing wiring).
- Domains/surfaces and their states.

## 3. In-flight
What is being worked RIGHT NOW, by whom, and what to verify when each lands.

## 4. Backlog (priority order)
The next actionable items with enough context to start each. Deep specs stay in their own docs — link.

## 5. Environment quirks & access
Pointers only (atlas + creds by key name), plus the quirks that burn cold agents.

## 6. Hard-won gotchas
The "do not relearn these" list. Promote recurring ones into `DOCTRINE.md` §6 or the global gotcha docs.

## 7. Session narrative (compressed)
A few sentences of how the project got here — enough to reconstruct intent, not a diary.
````
<!-- /TPL -->

### 4.5 `PROJECT/ADR/README.md`
<!-- TPL:PROJECT/ADR/README.md -->
````markdown
# ADR/ — decision records

> canonical prose of record · owner: whoever makes the decision (leader stamps in campaigns) · update: append-only

## The contract
1. **Every directed or architectural decision gets an ADR** — including rejected directions and
   deliberate deferrals. If it changed what we build or how, it's a decision.
2. **Numbering is gap-free and ascending** (`NNNN`, matching the hub `adr` entity number —
   `hubaudit` enforces contiguity). Check the highest existing number AND the hub before minting.
   A collision is repaired with a CORRECTION note, never by renumbering.
3. **Paired recording, same session:** the markdown file here is the full prose of record; a hub
   `adr` entity with the same number/title/status is recorded in the same working session (the hub
   is canonical for status and links; a stub entity with real prose only here is the recorded
   failure mode — don't repeat it, put real context/decision/consequences in both).
4. **Append-only.** Accepted context/decision text is immutable. Evolve via a dated
   `**Amendment (YYYY-MM-DD):**` block or full supersession (`superseded_by` both ways; a
   superseded ADR without a successor fails audit).
5. **Rejected alternatives stay on record.** The roads not taken — and WHY — are half the value.
6. **Status vocabulary** (mirrors `schema/adr.schema.json`): `proposed · accepted · superseded ·
   deprecated · rejected`.

## File naming
`NNNN-short-kebab-title.md` — start at `0001` (0000 is the template).
````
<!-- /TPL -->

### 4.6 `PROJECT/ADR/0000-template.md`
<!-- TPL:PROJECT/ADR/0000-template.md -->
````markdown
# ADR-0000 — <title: the decision, stated as a fact>

**Status:** proposed | accepted | superseded by ADR-NNNN | deprecated | rejected · YYYY-MM-DD ·
directed by <operator | leader | seat> · scope: <what this governs>

## Context
Why a decision was needed: the forcing situation, constraints, and the evidence (link research/
incidents/directives that raised it).

## Decision
The decision itself, stated so an agent can act on it without reading anything else.

## Consequences
What this makes true going forward: what gets easier, what gets constrained, what work it creates
(link the hub tasks it spawns), what it invalidates.

## Rejected alternatives
Each considered road not taken, with the concrete reason it lost. Preserve dissent.

## Target end-state (optional, for deliberately-pragmatic slices)
If this decision is a stepping stone, name the full solution it defers and what would trigger it.

<!-- Amendments append below, never edit above:
**Amendment (YYYY-MM-DD):** <what changes; this block is authoritative over the text above.>
-->
````
<!-- /TPL -->

### 4.7 `PROJECT/research/README.md`
<!-- TPL:PROJECT/research/README.md -->
````markdown
# research/ — deep research & review corpus

> canonical · owner: any seat producing research · update: one file per campaign/memo; chronicle updated same session

## The contract
1. **Research precedes build** (`../DOCTRINE.md` §4.1). No architectural task starts until its
   dependent research is captured here — findings living only in chat are lost work; mine prior
   chat/session history for findings and file them here before they evaporate.
2. **One file per effort**, named `YYYY-MM-DD-<slug>.md`. Genres this folder holds:
   - **Deep-research dossiers** — multi-agent research passes; keystone findings flagged.
   - **MoE panel reviews** — adversarial multi-expert reviews; keep verdicts AND explicit
     rejections/refutations (respect them later — they are anti-rework armor).
   - **Improvement-surface memos** — periodic step-back "how do we improve the whole system"
     essays with prioritized keystones.
   - **Domain audits** — focused investigations that end in a design recipe.
3. **`RESEARCH-HISTORY.md` is the chronicle** — the front door to WHY the architecture is what it
   is. Every research file gets an entry (date, question, keystones, decisions it fed → ADR ids)
   in the same session it lands.
4. **Findings that gate work become hub `gap` entities**; decisions become ADRs; the research file
   is their evidence link — not a task tracker itself.
````
<!-- /TPL -->

### 4.8 `PROJECT/research/RESEARCH-HISTORY.md`
<!-- TPL:PROJECT/research/RESEARCH-HISTORY.md -->
````markdown
# RESEARCH HISTORY — the chronicle of WHY

> canonical · owner: leader (or principal agent) · update: same session as any research/decision lands

This is the running answer to "why is the architecture what it is". Newest first. Every entry:
what question was open, what was found (keystones ⚑), and what it decided (→ ADR ids). Full detail
lives in the per-effort files in this folder; this file is the index a cold agent can actually read.

<!-- Entry format:
## YYYY-MM-DD — <question / effort title>
**Source:** <file in research/ | chat-mined | inline> · **Fed:** ADR-NNNN, gap ids
⚑ <keystone finding, one line each>
<2–5 sentences of what was learned and what it changed.>
-->
````
<!-- /TPL -->

### 4.9 `PROJECT/registers/FAILURE-MODES.md`
<!-- TPL:PROJECT/registers/FAILURE-MODES.md -->
````markdown
# FAILURE MODES — defect-class taxonomy → repair routing map

> canonical · owner: leader (any seat proposes rows) · update: when a repeated or novel failure class improves repair routing

**The doctrine:** an observed real failure becomes a fresh repair task and an `INCIDENTS.md` row.
Classify it here when doing so improves routing or reveals a repeated cause; do not delay the fix to
invent taxonomy. The successful retry of the failed operation is the default proof. No incident
creates a permanent test, fixture, scanner, or workflow. A rare critical boundary may use a
one-shot temporary diagnostic probe, whose receipt is retained after the probe is deleted before
commit. Repeated failures may be routed to a dedicated repair agent so delivery work keeps moving.

Row id = `FM-<group letter><n>`. Suggested starting groups (rename/extend to fit the domain):

## A — Identity / duplication
| # | Class | Seen? | Repair route / durable receipt |
|---|---|---|---|

## B — World drift (reality changed, we didn't)
| # | Class | Seen? | Repair route / durable receipt |
|---|---|---|---|

## C — Pipeline / ingest
| # | Class | Seen? | Repair route / durable receipt |
|---|---|---|---|

## D — Model judgment (agent/SLM errors)
| # | Class | Seen? | Repair route / durable receipt |
|---|---|---|---|

## E — Derivation / display (asserted ≠ derived)
| # | Class | Seen? | Repair route / durable receipt |
|---|---|---|---|

## F — Boundary / scope
| # | Class | Seen? | Repair route / durable receipt |
|---|---|---|---|

## G — Security / abuse
| # | Class | Seen? | Repair route / durable receipt |
|---|---|---|---|

## H — Process / governance (false-green, done≠live, ledger drift)
| # | Class | Seen? | Repair route / durable receipt |
|---|---|---|---|
````
<!-- /TPL -->

### 4.10 `PROJECT/registers/INCIDENTS.md`
<!-- TPL:PROJECT/registers/INCIDENTS.md -->
````markdown
# INCIDENTS — defect/incident instance ledger

> canonical · owner: whoever detects (leader confirms route) · update: at detection, again at resolution · append-only

Every observed real failure gets a row at detection time — including process failures (a false
green, a deploy collision, a fabricated artifact) and near-misses — plus a fresh repair task. Use
a `FAILURE-MODES.md` class when it improves routing; a novel incident may be repaired first and
classified during the same task. Close the incident when the failed real operation succeeds. Do
not create a permanent test, fixture, or workflow as a closure condition. If a critical boundary
needed a one-shot temporary probe, retain only its receipt and delete the probe before commit.

| ID | Date | Class (FM-, if useful) | What happened (one line) | Detected by | Repair task / route | Successful retry or critical receipt |
|---|---|---|---|---|---|---|
````
<!-- /TPL -->

### 4.11 `PROJECT/registers/TRUTH-MATRIX.md`
<!-- TPL:PROJECT/registers/TRUTH-MATRIX.md -->
````markdown
# TRUTH MATRIX — critical factual claims vs their derivation

> canonical · owner: worker maintains · update: when a machine-derived factual claim or its source changes

**The contract (DOCTRINE §2.2):** record only machine-derived factual claims whose incorrect value
could mislead an operator—for example task status, ownership, ordering, or counts. Editorial copy,
wording, visual style, spacing, color, and animation polish are explicitly outside this matrix and
must not receive automated copy validation or a test ceremony. The successful real operation is
the default proof for a changed derivation.

## Fields
| Field | Derivation (source of truth) | Real operation that exposes it | Durable completion receipt |
|---|---|---|---|

## Surfaces
List only surfaces that expose a critical factual claim. A surface does not need a row merely
because its copy or styling changed. Completed dependency receipts are inherited; a release may
observe only a newly created critical integration seam and must not replay each field's proof.

## Critical-boundary receipts

If security, destructive-data, migration, protocol, or concurrency risk truly requires more than
the real operation, create one temporary probe outside the durable product tree, run it once, file
the receipt here or in the Hub, and delete the probe before commit. Permanent tests, fixtures,
scanners, and verification workflows are prohibited.
````
<!-- /TPL -->

### 4.12 `PROJECT/registers/BLINDSPOTS.md`
<!-- TPL:PROJECT/registers/BLINDSPOTS.md -->
````markdown
# BLIND SPOTS — signals we do not capture (yet)

> canonical · owner: any seat · update: whenever a "we can't know that" moment occurs; review at each improvement pass

The standing "what are we systematically not measuring/capturing" register. Truth is time-stamped,
not timeless — most blind spots are temporal or provenance signals. A blind spot that starts
gating work becomes a hub `gap`; one that changes architecture becomes an ADR.

| ID | Signal we're blind to | The "tell" that exposed it | What we'd capture | Status (new / designed / building / wired) |
|---|---|---|---|---|
````
<!-- /TPL -->

### 4.13 `PROJECT/registers/DECISIONS-PENDING.md`
<!-- TPL:PROJECT/registers/DECISIONS-PENDING.md -->
````markdown
# DECISIONS PENDING — the operator queue

> canonical · owner: leader curates, operator resolves · update: on raise and on resolution · append-only rows

The routing target for the ONLY things agents may wait on (DOCTRINE §1.2): irreversible acts,
privileged/undefined-secret operations, and true operator-only choices. Raising a `DP-` entry never
stalls other work — route around it. Every entry carries a recommendation and an explicit
default-if-unanswered so the queue can drain without a meeting. Resolution becomes an ADR.

| ID | Raised | Question | Options + recommendation | Default if unanswered (and when it triggers) | Resolved → |
|---|---|---|---|---|---|
````
<!-- /TPL -->

### 4.14 `PROJECT/registers/GLOSSARY.md`
<!-- TPL:PROJECT/registers/GLOSSARY.md -->
````markdown
# GLOSSARY — ID schemes & terms of art

> canonical · owner: any seat · update: BEFORE first use of a new namespace or coined term

A newcomer must be able to decode every identifier and coined phrase in this project from this one
page. The framework-standard namespaces are in `../README.md` §4 — list here only what this project
adds (task-id phase prefixes, domain codes, detector names, artifact tags), plus domain terms.

## ID namespaces (project-specific)
| Prefix / pattern | Meaning | Home |
|---|---|---|

## Terms
| Term | Meaning |
|---|---|
````
<!-- /TPL -->

### 4.15 `PROJECT/audit/README.md`
<!-- TPL:PROJECT/audit/README.md -->
````markdown
# audit/ — filed point-in-time audit artifacts

> canonical artifacts · owner: whoever runs the audit · update: file the artifact the moment the audit completes · append-only

The continuous audit spine lives elsewhere (`../README.md` §2: the hash-chained hub ledger, deploy
entities, `verify/gate/`, `runs/`). THIS folder holds dated, point-in-time audit products:

- **MoE finding registers** — `moe-register-YYYY-MM-DD.json` (counts + register rows with
  id/title/experts/status/evidence/priority; keep the refuted findings — they are anti-rework armor).
- **Review verdicts / panel reports** — the filed output of any multi-expert review (prose companion
  goes in `research/`).
- **Security / dependency / data audits** — dated snapshots with their inputs named.
- **`hubaudit` snapshots** — optional filings of notable runs (first green, a RED that blocked a ship).

## Provenance rules
1. Every artifact self-describes: who/what ran it, when, exact inputs (SHA, data snapshot, manifest
   hash), and the rule/roster version it applied. An artifact you can't reproduce is testimony, not audit.
2. Artifacts are immutable once filed. Corrections = a new artifact referencing the old.
3. A wrong/fabricated artifact is VOIDED per `../README.md` §5 (moved to `_archive/voided-<ts>/`
   + a `void` event) — the void trail is audit history too.
````
<!-- /TPL -->

### 4.16 `PROJECT/verify/README.md` — the independent-verification contract
<!-- TPL:PROJECT/verify/README.md -->
````markdown
# verify/ — transient critical-boundary proof contract

> canonical contract · owner: leader · update: only when the critical-boundary policy changes

This directory defines an exceptional lane, not a standing test system. The successful real
operation is the default proof for every task. Copy, wording, style, spacing, color, animation
polish, routine fixes, and other non-critical changes never activate this lane and receive no
automated validation.

Permanent tests, fixtures, verifier scripts, calibration sets, generated manifests, scheduled
runs, and CI verification workflows are prohibited. A release is not automatically a reason to
verify completed work again.

## 1. Activation boundary

Activate a transient verifier only when the leader names a concrete risk at one of these critical
boundaries:

- security or privilege;
- destructive writes or data integrity;
- schema or data migration;
- public protocol compatibility;
- concurrency, leases, or fencing.

If the changed behavior can be safely exercised through its real operation, do that and stop. A
temporary probe is justified only when the real operation cannot expose an unacceptable failure
clearly enough.

## 2. One-shot procedure

1. Inherit the accepted receipts of every completed dependency. Never rerun child proof.
2. Name only the newly created critical integration seam, if one exists.
3. Prefer the real protected operation. If necessary, create one probe in system temporary space
   or explicitly disposable task scratch—never as a tracked project file.
4. Run it once against the final change and record a durable receipt containing the task, boundary,
   action or exact command, target SHA/state, verifier identity, observed outcome, and timestamp.
5. Delete the probe, fixture data, copied database, and all scratch before commit. Confirm only the
   receipt remains, then fold the verifier seat.

Receipts compose upward. A parent task or release accepts completed child receipts and, at most,
observes the one new critical seam created by composition. A verifier must never launch another
verifier or create checker fan-out.

## 3. Failure routing

An observed real failure is all the notice the project needs. Open a fresh repair task with the
failed action and observed outcome. Route it to a dedicated error-fixing agent when the project has
one so delivery agents continue unrelated work. After the repair, retry the failed real operation;
its successful result closes the task. Do not preserve the diagnostic as a regression test.

## 4. Stop rule

When the changed real behavior succeeds, no critical boundary remains unobserved, the durable
receipt is filed if one was required, and all temporary proof artifacts are gone, stop. More checks
reduce throughput and are a process defect.

`MANIFEST-CONTRACT.md` is dormant reference material unless a leader explicitly scopes it for a
single critical boundary. It does not authorize a standing generator, sweep, harness, or gate.
````
<!-- /TPL -->

### 4.17 `PROJECT/verify/MANIFEST-CONTRACT.md`
<!-- TPL:PROJECT/verify/MANIFEST-CONTRACT.md -->
````markdown
# MANIFEST CONTRACT — producer ↔ verifier interface

> template → canonical once filled · owner: PRODUCER (worker) — the verifier never writes this file · update: versioned amendments; regenerations bump `manifest_sha`

## Generation
`<command that regenerates the manifest>` → `verify/manifest.jsonl` + prints `manifest_sha`
(content hash of the generated file). Regenerate after every ship-relevant change; announce on the
bus so in-flight sweeps re-key.

## Row shape
```json
{"record_id": "<stable id>", "field": "<claim name>", "rendered_value": "<exactly what the user sees>",
 "evidence": [{"doc_id": "<source id>", "text": "<the gathered text>"}], "manifest_sha": "<hash>"}
```
- One row per (record, field) the product asserts.
- `rendered_value` is the USER-VISIBLE value (post-derivation), not the raw DB field.
- `evidence` includes EVERY evidence type the system holds for the claim — omitting one
  manufactures false "insufficient" verdicts systematically.

## Field inventory
| field | What it asserts | Derivation (must match `../registers/TRUTH-MATRIX.md`) | Evidence doc types included |
|---|---|---|---|

<!-- Amendments append below with dates; the newest block is authoritative. -->
````
<!-- /TPL -->

### 4.18 `PROJECT/runs/README.md`
<!-- TPL:PROJECT/runs/README.md -->
````markdown
# runs/ — machine-readable run ledger

> contract → canonical once a runner is wired · owner: whatever runs (pipelines, gates, batch jobs write here) · update: one artifact per run, written by the run itself · append-only

The base scaffold defines this artifact shape but does not ship a generic run recorder. A project
must wire its own pipeline/gate/batch tools before anything in this directory is operational.

- **One JSON per run:** `<UTC-stamp>.json` =
  `{run_id, kind, actor, started, finished, dry_run, stages:[{name, seconds, delta, error}],
    final_counts, errors, notes}`. Written by the tooling, never by hand. Long logs may sit
  alongside as `<run_id>-<stage>.log`.
- **`status.json`** = the current health rollup project-specific deploy gates may read:
  `{ok, violations, classes:{…}, at, run_id}` — regenerated by the invariant/gate check, never edited.
- Rows are never deduplicated or rewritten; a bad run stays on the ledger (that's the point).
  Voiding follows `../README.md` §5.
````
<!-- /TPL -->

### 4.19 `PROJECT/worklogs/README.md`
<!-- TPL:PROJECT/worklogs/README.md -->
````markdown
# worklogs/ — execution logs with numbers

> canonical · owner: the executing seat · update: append entries as work happens (not retrospectively) · append-only

One file per workstream: `<slug>.md`. The worklog is the "what actually happened, with measured
before/after" companion to any plan — plans claim, worklogs prove.

Entry discipline:
- Open with a **baseline snapshot** (the numbers before you touch anything).
- One dated entry per meaningful action/run: what ran, the metrics delta, what was discovered,
  what was deferred (deferred items also land as hub tasks or `registers/` rows — a worklog is not a tracker).
- Numbers come from runs (`../runs/`), never from memory.
````
<!-- /TPL -->

### 4.20 `PROJECT/ops/INFRA-INVENTORY.md`
<!-- TPL:PROJECT/ops/INFRA-INVENTORY.md -->
````markdown
# INFRA INVENTORY — deploy & ops runbook

> template → canonical once filled · owner: whoever touches infra · update: same session as any infra change; re-verify the "verified" stamp when read cold

**Verified against real config files on: <date> by <who>** — a runbook that hasn't been re-verified
against source is a rumor. Secrets stay in your organization's credential store BY KEY NAME
(e.g. a git-ignored `secrets.local` file plus a deploy-access atlas doc); this file holds structure, never values.

## Process & boot
How the app runs (server, workers, migrate-on-boot), and the exact boot order.

## Deploy paths
- **Code:** command, owner (campaigns: seat per `../pm/PROTOCOL.md` §7), gates it must pass, expected
  duration + the patience notes (what a "hung" deploy actually is).
- **Data:** command, owner, pre-ship gates, the stop/swap/start window behavior.
- **Sequencing law:** code-first when a change spans both (new code tolerates old data; old code
  on new data produces user-visible lies).

## Environment variables
| Var | Purpose | Notes (key name in creds, never the value) |
|---|---|---|

## Storage & mounts
Persistent paths, ownership/uids, backup story (and its verification date).

## Front door & TLS
DNS → edge → tunnel/origin chain; canary URLs; cache rules; the UA/CF gotchas that false-green canaries.

## Recovery
The failure modes this infra has actually had (link `../registers/INCIDENTS.md`) and the proven
recovery sequence for each.
````
<!-- /TPL -->

### 4.21 `PROJECT/pm/PROTOCOL.md` — the multi-agent campaign law
<!-- TPL:PROJECT/pm/PROTOCOL.md -->
````markdown
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

- **Interruptible points:** seats re-check their inbound monitor between atomic units and at
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
- **Continuously:** monitor armed; `blocked`/`question`/`proposal` answered within minutes (an
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
````
<!-- /TPL -->

### 4.22 `PROJECT/pm/seats/LEADER/CHARTER.md`
<!-- TPL:PROJECT/pm/seats/LEADER/CHARTER.md -->
````markdown
# LEADER CHARTER — v1 (<date>)

> template → canonical when a campaign activates · authored by: operator or outgoing leader · superseded whole, never edited

## Role
You are the LEADER (`../../PROTOCOL.md` §1). You orchestrate; you do not race your seats to
implementation. Your output is: correct sequencing, fast unblocking, verified credit, crystallized
governance, and safe deploys.

## Duties (non-negotiable)
1. **Classify verification proportionally** (PROTOCOL §6). Accept truthful implementer evidence for
   routine work; dispatch a fresh closer for declared boundaries and occasional samples. Never call
   self-verification independent.
2. **Answer `blocked`/`question` within minutes** — arm the STATUS monitor before anything else.
3. **Keep the ledger LIVE** (PROTOCOL §11) — your personal, non-delegable duty: the hub is the
   source of truth and every task/ADR/document is updated at the moment of the event, with full
   perfectionistic effort. No directive without a task; no decision without a real-prose ADR; no
   done without verified evidence; no deploy without its entity. A lagging ledger = you are failing the seat.
4. **Steer and discipline** (PROTOCOL §9): run the leader cadence (per-session ledger-parity
   sweep; sampled intermediate audit only on cadence or drift signal); watch for acceptance/scope/
   quality/behavioral drift; apply the ladder proportionally (NUDGE → CORRECTION → CHARTER
   AMENDMENT → SEAT RESET); run the silence/anti-thrash/runaway watchdogs; adjudicate `proposal`s
   and `finding`s within minutes — a seat waiting on you is a leader defect.
5. **Own CODE deploys** (unless re-chartered): gate-green precondition, mutex, patient canary, live verification.
6. **Stamp gates** — verifier artifacts are provisional until your `Leader-verified:` line.
7. **Keep `../../../HANDOFF.md` current** — you own project continuity.
8. **Route operator posts** — misrouted `OP-` orders get a HOLD + re-route, never silent drift.

## Write scope
`../../../HANDOFF.md`, all `seats/*/CHARTER.md` + `seats/*/DIRECTIVES.md` (append-only), STATUS
appends, hub writes, registers, ADRs. NOT: app code while seats own it, seat STATE files, verify/ internals.

## Current assignment
<the campaign's goal, active priorities, and deploy-ownership map — filled at spin-up>
````
<!-- /TPL -->

### 4.23 `PROJECT/pm/seats/WORKER-1/CHARTER.md`
<!-- TPL:PROJECT/pm/seats/WORKER-1/CHARTER.md -->
````markdown
# WORKER-1 CHARTER — v2 (<date>)

> template → canonical when a campaign activates · authored by: leader · superseded whole, never edited

## Role
You are WORKER-1 (`../../PROTOCOL.md` §1): you implement code, migrations, data work, and product
improvements—driving the directive queue and backlog to done with maximum useful throughput.

## Duties (non-negotiable)
1. **Monitor your `DIRECTIVES.md`** (stat-poll, armed at spin-up); read it before starting AND
   after finishing every task; never write to it.
2. **Report on the bus** (PROTOCOL §4): start/progress/done-with-evidence/blocked-with-tried/
   question-then-move-on/15-min heartbeats with real counts.
3. **Completion discipline:** perform the real operation, record its observed result, and stop.
   Copy, wording, style, motion, and non-critical changes receive no test or validation ceremony.
   Name any deploy the work needs (`deploy_request`)—done ≠ live.
4. **No permanent tests:** do not add test files, fixtures, checker scripts, or verification
   workflows. Only an explicitly critical security, destructive-data, migration, protocol, or
   concurrency boundary may use a one-shot probe; keep it outside the durable tree, run it once,
   retain the receipt, and delete it before commit.
5. **Defect discipline** (DOCTRINE §3): an observed failure becomes a fresh repair task. Retry the
   failed real operation after the fix; route repeated failures to a dedicated repair lane when one
   exists so planned delivery keeps moving.
6. **Own DATA deploys** (unless re-chartered): code-first sequencing, mutex, actual ship operation,
   and scoped kills only. Completed child receipts are inherited; inspect only a new critical
   integration seam.
7. **Update `STATE.md`** after every batch — any interruption must be free.
8. **Consume repair tasks or auto-routed critical `alert`s** for established classes directly
   (PROTOCOL §8.4), without turning them into a standing validation lane.
9. **Honor the interrupt contract** (PROTOCOL §5): re-check your monitor between atomic units and
   ≥ every ~10 min inside long ones; on `🔴`/`🛑` — checkpoint `STATE.md`, post `preempted`,
   comply, resume. Operator posts outrank everything.
10. **Propose before deviating** (PROTOCOL §9.5): any departure from a directive — including
    improvements — is a `proposal` FIRST; premise-changing discoveries are `finding`s the moment
    they're grounded; a directive that violates DOCTRINE/CHARTER gets challenged before execution.

## Write scope
App code and data tooling, temporary critical-boundary scratch that is deleted before commit, hub
writes for your tasks, register rows you originate, your `STATE.md`, and STATUS appends.
NOT: permanent tests/fixtures/verifier workflows, other seats' files, directives channels,
verifier receipts, or CODE deploys.

## Current assignment
<queue source + priorities — filled at spin-up>
````
<!-- /TPL -->

### 4.24 `PROJECT/pm/seats/VERIFIER/CHARTER.md`
<!-- TPL:PROJECT/pm/seats/VERIFIER/CHARTER.md -->
````markdown
# VERIFIER CHARTER — v1 (<date>)

> template → canonical when a campaign activates · authored by: leader · superseded whole, never edited

## Role
You are the VERIFIER (`../../PROTOCOL.md` §1, contract `../../../verify/README.md`): a disposable or
standing independent lane activated for an explicit boundary scope. For each claim in that scope,
determine whether it is supported by the system of
record, by the gathered evidence, and by reality — and surface everything that isn't. You are
authorized to distrust everything, including our own data.

## Duties (non-negotiable)
1. **Three lanes** per claim: derivation / evidence / world. Live checks save a `livecap/` snapshot or don't count.
2. **Grounding law:** every `supported` verdict quotes verbatim-contained text; run the mechanical
   selfcheck before writing any gate artifact.
3. **Write-before-report:** verdict rows land in `verdicts.jsonl` before you post about them.
4. **Gate artifacts** follow the versioned GREEN-RULE in `../../../verify/README.md` §4 — you never
   redefine green; zero means zero.
5. **Escalate, never fix:** findings are `alert` events with grounded evidence; you never patch
   code or data.
6. **Anti-stall** (PROTOCOL §10): cap per-record effort (~2 min), cap fetches (~20 s), never retry
   a tool more than twice, close `insufficient` and move on; confusing directive → `question` +
   continue everything else.
7. **Position durability:** `STATE.md` after every batch, keyed on `(record_id, field)` +
   `manifest_sha` — never line offsets. Interruptions are non-events; no context narration.
8. **Honor the interrupt contract** (PROTOCOL §5): checkpoint + `preempted` + comply on `🔴`/`🛑`;
   propose before deviating from your sweep scope (PROTOCOL §9.5).
9. **Exit at the boundary:** publish the terminal verdict, checkpoint durable artifacts, and fold
   the seat immediately unless the charter explicitly defines another batch.

## Write scope
`../../../verify/**` EXCEPT `MANIFEST-CONTRACT.md` (producer-owned — v1 lost it to a verifier
overwrite once; never again), your `STATE.md`, STATUS appends. NOT: app code, seeds, data, other
seats' files, deploys, ssh. Read the DB only from your own copy under `verify/tmp/`.

## Current assignment
<sweep scope + calibration set + gate cadence — filled at spin-up>
````
<!-- /TPL -->

## §5 Notes on the campaign protocol

`pm/` stays dormant in SOLO mode — the Plane is complete for a single agent (hub + HANDOFF +
registers + gates). Activate seats per `pm/PROTOCOL.md` §0 only when a lane saturates. The
protocol's file-based bus (append-only + lock-retry appends + stat-poll monitors) is the proven
floor for agents that cannot message each other; if the target environment has a real addressable
bus with per-seat ACLs, adopt it by ADR — the event vocabulary and duties transfer unchanged.

## §6 Bootstrap procedure (fresh environment, step by step)

1. **Create the project repo/folder**; version-control it from the first commit.
2. **Instantiate `PROJECT/`** from §4, byte-exact, plus the `schema/` files from §3.1.
3. **Bind the substrate** (§2.2): when adopting from `hub-scaffold`, copy/mount the shipped
   `hub_core` and Django adapter. In another environment, map the entity model onto a suitable
   tracker/event system or implement the roles in §2.3. Record the mapping and all deviations as
   ADR-0002; do not represent a planned binding as operational.
4. **Activate the gate** (§2.4): run the reference `hubaudit` or implement an equivalent and wire it
   as a REQUIRED check (CI required status / pre-receive / pipeline gate). Advisory wiring is a
   bootstrap failure.
5. **Genesis, live-ledger style:** record ADR-0001 "This project adopts the Project Plane"
   (accepted, full prose) + the first real tasks — through the write path, never by editing
   projections.
6. **Fill `CHARTER.md` and cut the first `HANDOFF.md`.** Declare ID namespaces in
   `registers/GLOSSARY.md` if any beyond the standard set.
7. **Run §7.** Fix until green-with-proven-red. Only then start feature work.

## §7 Setup self-test (the Plane must fail correctly before it may pass)

This is the mutation-testing / fault-injection principle applied to governance — the same practice
as restore-testing a backup or trigger-testing a detection rule: **a control that has never been
observed to fail is presumed non-functional.** The rule is one seed per gate invariant; the seeds
below cover the reference gate (§2.4) one-for-one — if your binding adds invariants, add seeds.

Seed each violation, run `plane_audit`, and require the stated result; then remove the seeds and
require PASS. Record the whole run as the project's first `runs/` artifact.

| # | Seed | Required result |
|---|---|---|
| 1 | a task written `status: done` with no `verified_by` | write REJECTED (schema) — or audit HIGH if forced into the store |
| 2 | an entity with a dangling `idref` | audit HIGH |
| 3 | an ADR numbered with a gap (e.g. 1 then 3) | audit WARN |
| 4 | one byte modified in a mid-file ledger event | audit CRITICAL (chain) |
| 5 | a gate artifact hand-edited to `green: true` over refuted rows | when the independent verification lane is implemented, its consumer re-derivation flags FABRICATED-GREEN and blocks |
| 6 | a completion attempted without a live claim (multi-agent binding) | write REJECTED |
| 7 | clean state (all seeds removed) | audit exit 0 |

Behavioral spot-checks: projections regenerate identically from the ledger after deletion. If the
file-channel protocol is activated, an editor-style rewrite is detectable by its monitor. If the
project run recorder is implemented, the audit run appears in `runs/`. Mark unimplemented optional
lanes as such; absence may be an accepted bootstrap scope, fabricated green may not.

## §8 Rebinding quick-reference (work environments)

| Plane concept | GitHub-centric | Jira/Linear-centric |
|---|---|---|
| Hub entities | Issues + labels + required templates; CI job validates against §3 schemas on every mutation via export | native items + required fields; scheduled export → schema validation |
| Ledger (R1) | signed commits on a `plane-ledger` branch (append-only JSONL) | same JSONL ledger in-repo (the tracker is a projection) |
| Gate (R3) | required status check running `plane_audit` | pipeline gate; merge blocked on exit≠0 |
| Deploy entities | written by the deploy workflow, never by hand | same, from the CD system |
| pm channels | in-repo `PROJECT/pm/` files exactly as specified | same (the bus is files regardless of tracker) |

The tracker may be the *entity store* (R2) only if it can enforce the unsatisfiability rules at
write time; otherwise the ledger stays canonical and the tracker is a synced projection — declare
which in ADR-0002.

## §9 Hub Excellence Contract

<!-- TPL:PROJECT/HUB-QUALITY.md -->
````markdown
# HUB QUALITY — the construction contract

> canonical contract · owner: project operator · update: whenever the Hub's product, truth, flow, or coordination bar changes

This is the minimum and aspirational bar for every Hub surface. A Hub should be phenomenally useful,
visually unmistakable, alive with truthful realtime feedback, and tuned for extraordinary task
throughput. Quality is established by using the real thing. Permanent tests, copy assertions, and
ceremonial verifier ladders are not substitutes for an authored product.

## 1. Product and visual excellence

A Hub must have a distinctive project identity, deliberate hierarchy, strong spatial rhythm, clear
information density, and coherent depth, color, type, motion, and interaction. It must feel authored
for its project rather than like an interchangeable admin template. Delight is welcome when it makes
state, causality, or attention easier to understand; decoration must never compete with truth.

Every viewport has one dominant above-the-fold operational decision. A metric appears only once
above the fold. Each visual region has at most one persistent animated signal; all other motion is
caused by meaningful state change. Every generated Hub declares a project-specific mark, accent
pairing, display voice, surface character, and optional visual motif before visual construction.

When constructing a new Hub or making a material redesign, use the rendered product at its empty,
ordinary, dense, loading, live, degraded, and error states. Ask: where does the eye land, what needs
action, what changed, what is trustworthy, and can the next useful action be taken without hunting?
This is product work, not a demand for a permanent visual test suite.

## 2. Required invariants

### 2.1 Truth-derived UI

- The canonical snapshot/JSON island is the source of rendered assertions. DOM labels, totals,
  progress, delivery state, and animation derive from it; markup is not a second ledger.
- `done`, `landed`, `deployed`, and `live` remain separate claims with separate evidence. Unknown or
  unavailable evidence renders **unmeasured**, never zero, success, or false green.
- Every metric names its denominator, window, and freshness. Empty denominators remain unmeasured.

### 2.2 Realtime truth

- Realtime starts with a complete snapshot and monotonic cursor. SSE carries event identity, honors
  `Last-Event-ID`, and triggers an exact delta/snapshot read; reconnects are ordered and deduplicated.
- The UI names its current mode: **live**, **degraded polling**, or **manual refresh**. Silence is not
  proof of freshness. A local reference integration should visibly converge within two seconds unless
  the project records another SLO.
- Heartbeats, replays, and no-op deltas do not animate as work. Motion follows a real state transition.

### 2.3 Accessible, responsive interaction

- Target WCAG 2.2 AA. Preserve content and function at 320 CSS px without two-dimensional scrolling
  except where the content intrinsically requires it; never encode meaning by color alone.
- Every action is keyboard-operable with visible focus. Tabs use `tablist`/`tab`/`tabpanel`, one
  selected tab, roving focus, arrow navigation, Home/End, and Enter/Space where activation is manual.
- Modal dialogs put focus inside, contain Tab/Shift+Tab, close on Escape, make the background inert,
  have an accessible name, and restore focus to the invoker.
- Status changes are announced without stealing focus. Reduced-motion and forced-color modes retain
  equivalent meaning and functionality.

### 2.4 Motion grammar and layout stability

Animation communicates entry, transition, dependency, progress, or attention. It is interruptible,
does not endlessly celebrate ordinary activity, does not cause layout shift, and has a meaningful
reduced-motion form. Live patches preserve reading position and focus.

### 2.5 Performance and dependency floor

At the 75th percentile of field visits, target LCP <= 2.5 s, INP <= 200 ms, and CLS <= 0.1 on mobile
and desktop. Label field and lab evidence honestly; a lab run cannot establish a field pass. Record
project-specific bundle, request, and realtime budgets. The base Hub has no runtime CDN dependency;
an adopter exception requires an ADR, failure behavior, and offline/degraded proof.

### 2.6 Flow that improves throughput

Expose work in progress (started, not finished), throughput (finished per stated unit of time), work
item age, cycle-time distribution, and a service-level expectation expressed as period plus
probability. Also expose readiness, stalled/expired work, the dependency frontier, and arrival versus
departure pressure where measurable. Missing data is **unmeasured**. Do not turn raw worker counts or
ticket volume into a leaderboard; optimize finished value and bottleneck removal, not activity theater.

### 2.7 Durable agent coordination

Use one durable task lifecycle with atomic leases/fencing, idempotent mutation, heartbeats, explicit
ownership, resumable plans, and attached evidence. Bound work in progress by the ready frontier;
parallelize only independent work and return structured results. Carry task, agent, lease, and trace
correlation across boundaries. Advertise MCP, A2A, streaming, or other capabilities only when the
callable transport and behavior actually exist.

## 3. Proof without test accumulation

The default proof is the actual operation: make the change, exercise the changed path on the real
surface, and observe whether it works. If it breaks, that observed failure is the notice and becomes
fresh board input. The delivery agent records it without speculative repair or silently changing
roles; the operator may later route it to a dedicated repair/error-fixing lane.

- Do not create or run a test for copy, wording, spacing, color, ordinary style or animation tuning,
  or another non-critical narrow fix. Do not validate page copy with assertions, snapshots, pixel
  comparisons, screenshots-as-gates, or a second agent. Implement it on the page and move on.
- A test is justified only for a rare critical boundary such as security or authorization, destructive
  data integrity, a migration, public protocol compatibility, or concurrency/fencing. That test must
  be a one-shot transient probe in temporary storage, run only for the named risk, and deleted before
  commit. Retain its result as the task receipt; never retain the test artifact.
- A completed child task's receipt composes into its parent. Parents and releases inherit those
  receipts rather than rerunning child proof. A release may probe only the genuinely new integration
  seam created by composing the children.
- Never nest verifiers. A closer may not dispatch another closer, suite, or proof ladder. One boundary,
  one smallest decisive operation, one receipt, then it exits.

For a new Hub or material redesign, the following is a design-coverage guide, not a standing suite or
a requirement for every edit:

| Dimension | States to use when materially affected |
|---|---|
| Width | 320, 768, and 1440 CSS px |
| Theme | light and dark, where both are supported |
| User preference | normal motion, reduced motion, forced colors |
| Input | keyboard and pointer paths |
| Data/transport | empty, ordinary, dense, live update, degraded, and error |

**Stop rule:** once the real changed behavior succeeds and no critical boundary was crossed, record
the work and stop. Do not add a check, test, screenshot ritual, independent verifier, or release rerun
merely to make simple work look more proven.

## 4. Elevation workflow

1. Research current primary standards and the project's audience and visual identity.
2. Audit rendered states and live behavior; record concrete product defects.
3. Write a design brief naming hierarchy, tokens, motion grammar, state semantics, and budgets.
4. Implement from canonical data through the renderer, preserving local identity.
5. Use the affected real paths. Keep their receipts; create a transient probe only for a critical
   boundary, and remove that probe before commit.
6. Curate generally useful improvements back into `hub-scaffold`; never bulk-merge an instance.

Use `campaigns/elevate-hub.md` for the executable campaign. Exceptions belong in an ADR with owner,
expiry/revisit trigger, user impact, and evidence. Upgrades preserve project identity and local theme,
diff this contract explicitly, and upsert generic units without imposing redundant proof work.

## 5. Primary standards

- [WCAG 2.2](https://www.w3.org/TR/WCAG22/)
- [WAI-ARIA tabs](https://www.w3.org/WAI/ARIA/apg/patterns/tabs/)
- [WAI-ARIA modal dialogs](https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/)
- [Core Web Vitals thresholds](https://web.dev/articles/defining-core-web-vitals-thresholds)
- [WHATWG server-sent events](https://html.spec.whatwg.org/dev/server-sent-events.html)
- [The Kanban Guide](https://kanbanguides.org/the-kanban-guide/)
- [DORA metrics](https://dora.dev/guides/dora-metrics/)
- [OpenAI Agents SDK orchestration](https://openai.github.io/openai-agents-python/multi_agent/)
- [MCP Tasks](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks)
- [A2A specification](https://a2a-protocol.org/dev/specification/)
- [OpenTelemetry overview](https://opentelemetry.io/docs/specs/otel/overview/)
````
<!-- /TPL -->
