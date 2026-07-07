# THE PROJECT PLANE — BOOTSTRAP SPECIFICATION (v1, 2026-07-02)

**This document is fully self-contained.** Hand it to a fresh agent in any environment — no other
files, repos, or credentials from the origin ecosystem are needed — and it can stand up the
complete Project Plane: a verifiable, agent-operated project-management system with a
tamper-evident source of truth, live governance, independent verification, and a crystallized
multi-agent protocol.

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
5. Everything in this spec is law unless your environment truly cannot provide it — in which case
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

### 2.3 Reference substrate (pure files — works anywhere)

If the environment provides nothing, implement this; it needs only a filesystem and any scripting
runtime.

**Ledger** — `PROJECT/.plane/ledger.jsonl`, one event per line, append-only:

```json
{"seq": 12, "ts": "2026-07-02T18:00:00Z", "aggregate": "myproj:task:0007",
 "type": "task.updated", "payload": {"status": "in_progress"},
 "prev_hash": "<hash of event 11>", "hash": "<see below>"}
```

- `hash` = SHA-256 hex of the canonical JSON (sorted keys, no whitespace, UTF-8) of the event
  **without** the `hash` field. Genesis event's `prev_hash` = 64 zeros.
- `verify_chain`: replay the file recomputing every hash and linking every `prev_hash`; any
  mismatch = tampering = CRITICAL.
- Event `type` vocabulary: `<entity>.created`, `<entity>.updated`, `<entity>.amended`,
  `task.claimed`, `task.completed`, `event.reverted` (a compensating event — the only way to undo).
- Writes are validated BEFORE append (R2): fold the aggregate's current state, merge the payload,
  validate against the schema — reject on any violation, including the conditional rules.

**Entities** — projections, never a store: fold events per aggregate in `seq` order,
last-write-wins per field. Any materialized view (JSON snapshot, dashboard, markdown table)
carries a "generated — canonical: ledger" header and is regenerated, never edited.

**Claims (multi-agent)** — `PROJECT/.plane/claims/<entity-id>.json` =
`{"task": id, "agent": seat, "token": nonce, "claimed": epoch, "expires": epoch}`; a completion
without a live claim is rejected; expired claims auto-release.

### 2.4 The gate runner (`plane_audit`)

One command; runs everywhere (CI required check + pre-ship + on demand); **fail-closed** (an
internal error is a RED, never a skip). Invariants, in order:

1. **Schema validity** — every folded entity validates, including the false-claim rules (§3). HIGH.
2. **Referential integrity** — every idref resolves; no dangling references. HIGH.
3. **ADR contiguity** — ADR numbers gap-free from 1; every `superseded` has a successor. WARN.
4. **Chain verification** — `verify_chain` intact. CRITICAL.
5. **Live-ledger parity** — created-vs-transitioned ratio and stale `in_progress` beyond a
   threshold flag a lagging ledger (the governance-fiction failure). WARN→HIGH.
6. **Build coherence** (once deploys exist) — last deploy's `sha` == intended == what the live
   artifact reports; unknown ⇒ AMBER, mismatch ⇒ HIGH.
7. **Behavioral adapters** (environment-specific, added over time) — e.g. settings safety,
   route-guard checks. An adapter that raises ⇒ CRITICAL.

Exit codes: `0` PASS · `2` blocking (critical/high) · `3` warn-only amber · `1` internal error
(treat as RED). The write path adds its own guards: `done` cannot be minted directly — only a
completion operation that requires a live claim, ≥1 evidence entry, and a passing verification
command may grant it.

## §3 The entity model

Seven entity types. The conditional (`if/then`) rules are the heart: **they make false claims
unsatisfiable at write time.** Field lists are authoritative in the embedded schemas (§3.1).

| Type | Required | Status enum | Unsatisfiability rules |
|---|---|---|---|
| `task` | id, type, title, status, version | `todo · in_progress · blocked · done · dropped · shadow` (shadow = wired-but-inert, NOT done) | `done` ⇒ `verified_by` ≥1 · `blocked` ⇒ `deps` ≥1 |
| `adr` | id, type, number, title, status, version | `proposed · accepted · superseded · deprecated · rejected` | accepted/superseded/deprecated ⇒ full `context_md`+`decision_md`+`consequences_md` · `superseded` ⇒ `superseded_by` ≥1 |
| `gap` | id, type, title, status, version | `open · investigating · mitigated · closed · wont-fix` | mitigated/closed ⇒ `addressed_by` ≥1 |
| `feat` | id, type, name, status, version | `shipped · partial · planned · experimental · removed` | shipped/partial ⇒ `tasks` ≥1 (no orphan feature claims) |
| `deploy` | id, type, sha, at, version | — (append-only record, written BY the act of deploying) | `audit_ok` computed from the gate exit, never hand-set; `served_sha` null until a canary confirms |
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
  "description": "HUB DOCTRINE shared $defs. Identical bytes across all project hubs. IDs are project-prefixed, type-segmented, allocated once, never reused/renumbered.",
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
      "description": "A pointer to recomputed proof: a test exit, a headless screenshot path, a /debug scorecard url, an audit violation id."
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
    "verification_command": { "type": "string", "description": "The command the HUB runs server-side to grant done; exit 0 + evidence required." },
    "touches": { "type": "array", "items": { "type": "string" }, "description": "Files/areas this task changes." },
    "plan": {
      "type": "array",
      "items": { "type": "object", "additionalProperties": false, "properties": { "step": { "type": "string" }, "done": { "type": "boolean" } }, "required": ["step", "done"] },
      "description": "Persisted, resumable checklist."
    },
    "deps": { "type": "array", "items": { "$ref": "hub:common#/$defs/idref" }, "description": "Blocked iff any dep is not done." },
    "implements": { "type": "array", "items": { "$ref": "hub:common#/$defs/idref" }, "description": "feat/cap this realizes." },
    "decided_by": { "type": "array", "items": { "$ref": "hub:common#/$defs/idref" }, "description": "ADR(s) governing this task." },
    "verified_by": { "type": "array", "items": { "type": "string" }, "description": "Verification evidence summaries; >=1 required for done." },
    "evidence_uri": { "type": "array", "items": { "$ref": "hub:common#/$defs/evidenceUri" } },
    "surfaced_by": { "$ref": "hub:common#/$defs/idref", "description": "The task/work during which this was scouted." },
    "source": { "type": "string", "description": "Where it came from, e.g. REVIEW-G3, CHARTER, RESEARCH-HISTORY." },
    "legacy_ref": { "type": "string", "description": "Pre-migration id, e.g. #V4.7 / A3." },
    "version": { "type": "integer", "minimum": 0, "description": "Per-aggregate OCC version." },
    "provenance": { "$ref": "hub:common#/$defs/provenance" }
  },
  "required": ["id", "type", "title", "status", "version"],
  "allOf": [
    { "if": { "properties": { "status": { "const": "done" } }, "required": ["status"] }, "then": { "properties": { "verified_by": { "type": "array", "minItems": 1 } }, "required": ["verified_by"] } },
    { "if": { "properties": { "status": { "const": "blocked" } }, "required": ["status"] }, "then": { "properties": { "deps": { "type": "array", "minItems": 1 } }, "required": ["deps"] } }
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
    "context_md": { "type": "string", "description": "Immutable post-accept." },
    "decision_md": { "type": "string", "description": "Immutable post-accept." },
    "consequences_md": { "type": "string" },
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
  "description": "A reviewed finding from the analysis (REVIEW plans, RESEARCH-HISTORY, the CHARTER, CAPABILITY-LEDGER 'Needs'). Materializing these into the hub is Phase 2.",
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
    "sha": { "type": "string", "description": "The blessed git SHA the receiver wrote." },
    "method": { "type": "string" },
    "at": { "$ref": "hub:common#/$defs/isoDate" },
    "audit_ok": { "type": "boolean", "description": "COMPUTED from the gate exit at deploy time, never hand-set." },
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
  "description": "A reusable system/architecture node in the cross-project capability fabric. Project-prefixed ids share ONE id space so cap graphs cross projects.",
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

### 3.2 Genesis seed (example shape)

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
  README.md  CHARTER.md  DOCTRINE.md  HANDOFF.md  seed.json  schema/(8 files)
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
# THE PROJECT PLANE — canonical PROJECT/ framework (v1, 2026-07-02)

> canonical · owner: whoever leads the project · update: only by ADR (this file is the framework spec)

Every app owns its code; **this folder owns everything about how the project is run**: decisions,
research, doctrine, gaps, verification, audit history, agent coordination. It is **content-agnostic**
— nothing in the framework refers to any particular app. It was crystallized from live-fire
multi-agent campaigns, the hub platform, and a hard-won doctrine corpus. Your organization's global
doctrine documents (lifecycle framework · method playbook · quality charter) are home-ecosystem
bindings, replaceable per §7. Where any older doc lists legacy PROJECT/ file sets
(TASKS/FEATURES/CHANGELOG/DEPLOYS markdown), **this manifest supersedes them**: those facts live in
the hub ledger now.

## 0. Read-order for a cold agent

1. **`HANDOFF.md`** — you-are-here: current state, in-flight work, quirks. Always first.
2. **`CHARTER.md`** — what this project is, its quality bar, its definition of done.
3. **`DOCTRINE.md`** — the standing laws you must not violate.
4. **The hub** — `python manage.py hubaudit` + `/hub` (or fold `PROJECT/.hub/events.jsonl`) for
   canonical tasks/ADRs/gaps/features/deploys.
5. **`pm/PROTOCOL.md`** — only if a multi-agent campaign is active (HANDOFF says so).

## 1. The manifest

| Path | Artifact class | Canonical? |
|---|---|---|
| `README.md` | the framework spec + map | canonical (framework) |
| `CHARTER.md` | mission · scope · quality bar · definition of done | canonical |
| `DOCTRINE.md` | standing laws (operator contract + crystallized project laws) | canonical |
| `HANDOFF.md` | living continuity file — the single resume entry point | canonical, always current |
| `seed.json` · `schema/` · `.hub/` | hub genesis · entity schemas · hash-chained event ledger | `.hub/events.jsonl` = THE ledger |
| `ADR/` | numbered decision records (full prose of record) | canonical prose; hub `adr` entity canonical for status/links |
| `research/` | deep research: dossiers, MoE panels, improvement-surface memos + `RESEARCH-HISTORY.md` chronicle | canonical |
| `registers/` | what hub schemas don't model: failure-mode taxonomy, incidents, truth matrix, blind spots, pending operator decisions, glossary | canonical |
| `audit/` | filed point-in-time audit artifacts (MoE registers, audit runs, security reviews) | canonical artifacts |
| `verify/` | independent-verification harness: manifest ↔ verdicts ↔ fail-closed gate | canonical (contract in its README) |
| `runs/` | machine-readable run ledger + current gate-status rollup | canonical |
| `worklogs/` | per-workstream execution logs with measured before/after | canonical |
| `ops/` | infra inventory / deploy runbook | canonical, date-stamped |
| `pm/` | multi-agent campaign kit: protocol, seats, channels | channels = operational log, NOT a governance store |

**Not in this folder:** tasks, gaps, features, deploys, capabilities — those are **hub entities**
(schema-validated, hash-chained, audit-gated). Markdown renderings of hub data are views and must
say so (see §3).

## 2. Where the audit history lives (the user-visible answer to "what happened?")

- **`.hub/events.jsonl`** — the tamper-evident spine: every task/ADR/gap/deploy transition,
  SHA-256 hash-chained, append-only. `hubaudit` verifies the chain + schema + referential integrity
  + build coherence, fail-closed.
- **Hub `deploy` entities** — one per deploy, keyed SHA+timestamp, appended unconditionally,
  `audit_ok` computed never hand-set.
- **`audit/`** — dated point-in-time audit artifacts (MoE finding registers, review verdicts).
- **`verify/gate/`** — fail-closed ship-gate artifacts with versioned green rules.
- **`runs/`** — one JSON per operational run; `runs/status.json` = the current green/red rollup.
- **`registers/INCIDENTS.md`** — every defect instance: class, detection, resolution, detector born.

## 3. Source-of-truth law

1. **The hub is THE source of truth for all trackable state** — tasks, ADRs, gaps, features,
   deploys, capabilities, notes. One canonical store per fact class: hub = entities; markdown =
   prose + the registers above; channels (`pm/`) = coordination traffic only. Anything that
   contradicts the hub is wrong until the hub is amended.
2. **Every file opens with a role header**: `> canonical | view (source: X) | channel | template`
   plus owner and update trigger. A view that could be mistaken for canon is a defect.
3. **Views declare and never lead.** A rendered table of hub data carries
   `RENDERED VIEW — canonical: hub` and is regenerated, never hand-drifted.
4. **The ledger is LIVE.** Entity transitions are recorded at the moment of the event (claim →
   `in_progress`, decision → ADR, verified → `done`+evidence, deploy → deploy entity) — never
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
| run ids (`<UTCstamp>` / `<scope>-v<n>`) | runs and gate artifacts | `runs/` · `verify/gate/` |

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
MAINTAIN), the multi-expert (MoE) review method, and the four false-green enforcement primitives
live in your organization's global doctrine documents (see the header). `DOCTRINE.md` carries the laws that must be in-context at all
times; everything else is subsumed by reference — do not re-paste global doctrine here.

## 7. Portability — rebinding the Plane to any environment

The framework is a set of **roles**, not tools. It runs anywhere that can provide three substrate
roles; everything else in this folder is plain files:

| Role the Plane requires | Home-ecosystem binding | Rebind to (examples) |
|---|---|---|
| **Tamper-evident append-only ledger** (entity transitions, hash-chained) | `hub_core` store → `.hub/events.jsonl` | any event store, signed git log, ledgered DB |
| **Schema-validated entity store with false-claim-unsatisfiable rules** (done⇒verified_by, etc.) | hub entities + `schema/*.json` + `seedhub` | Jira/Linear + required-field rules, GitHub Issues + CI schema check |
| **Fail-closed gate runner** (audit + invariant checks, exit≠0 blocks ship) | `manage.py hubaudit` + deploy gates | CI required checks, pre-receive hooks, pipeline gates |

Rebinding rules:
1. **Every path outside this folder is a BINDING, not a dependency.** The global-doctrine docs in
   the header, the deploy atlas, and hub commands are the home ecosystem's instances; when
   pivoting, replace them with the target environment's equivalents and re-point the citations —
   the laws in `DOCTRINE.md`, the registers, and `pm/PROTOCOL.md` transfer verbatim.
2. **The protocol's channel mechanics are substrate-independent** (`pm/PROTOCOL.md` §13): append-only
   files + monitors are the proven floor; any addressable bus with per-seat ACLs may replace them
   by ADR without changing the event vocabulary or duties.
3. **What may never be rebound away:** the verifier-identity invariant, fail-closed gates,
   re-derivation over trust, append-only history, and one-canonical-store-per-fact-class. An
   environment that can't provide these isn't a binding target — it's a gap.
````
<!-- /TPL -->

### 4.2 `PROJECT/CHARTER.md`
<!-- TPL:PROJECT/CHARTER.md -->
````markdown
# CHARTER — <project name>

> template → becomes canonical once filled · owner: leader · update: by ADR only (scope changes are decisions)

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
- **Born-safe:** prod settings hardened at birth (no DEBUG default, no committed secrets, token-gated writes).
- **Truth-first:** every rendered assertion derives from gathered evidence (`DOCTRINE.md` §2) —
  the truth matrix (`registers/TRUTH-MATRIX.md`) is the acceptance checklist for any new surface.
- **Gate-green:** `hubaudit` PASS + project invariant gates green are ship preconditions, fail-closed.
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

These are the laws every agent on this project operates under, regardless of content. They are the
distillation of every hard lesson to date. Violating one is a defect even when the output "works".

## §1 Operator contract
1. **Zero decisions pushed to the operator.** Best-guess every fork, record it (ADR if architectural,
   `DP-` entry if genuinely operator-only), and proceed. Asking permission to continue is a defect.
2. **Drive to done.** Once a goal is set, execute to completion. Pause only for: an irreversible or
   destructive act, a privileged/undefined-secret operation, or a true operator-only decision —
   and even then, queue it in `registers/DECISIONS-PENDING.md` and route around it.
3. **No device-test gates.** Never frame a milestone as "waiting on the operator to test".
   Implement full scope; real-device checks come at the end.
4. **Best way, no thrashing.** Research best-of-breed first; a named technology is a hypothesis,
   not a mandate; when an approach keeps failing, re-architect — don't polish.
5. **Verify every agent claim yourself.** No sub-agent/seat "done" is recorded until independently
   re-verified (re-run the stated command, read the diff, check the live wiring).
6. **Track and document, always.** Every directed change gets a hub task AND a decision record.
   Note every downstream artifact a shared-state change invalidates.

## §2 Truth discipline (anti-false-green)
1. **FALSE-GREEN is the meta-failure.** Gates fail by being self-attested, bypassed, textual, or
   committed-not-deployed — not by being absent. The four enforcement primitives (authorization-
   boundary hook · behavioral-not-textual audit · out-of-band deployed-artifact canary ·
   tamper-evident never-weaken invariants) are defined in the global charter; the portable
   invariant is: **the verifier identity must differ from the builder identity.**
2. **ASSERTED ≠ DERIVED = BROKEN.** Every rendered assertion (every label, badge, ordering, count)
   must derive from gathered evidence via a deterministic path. One mismatch anywhere means the
   product is broken. `registers/TRUTH-MATRIX.md` maps every field to its derivation and detector.
3. **Done ≠ live.** A task whose value requires a deploy is NOT done until the deploy-owner is
   notified (a `deploy_request` event naming code/data + SHA) and the deploy is verified live.
4. **Evidence must postdate the final edit.** A verification run from before the last change is void.
5. **Gates re-derive, never trust.** Any consumer of a gate artifact recomputes the verdict from
   the underlying rows; a green flag contradicted by its rows is FABRICATED-GREEN and blocks.
6. **Every gate must have failed in test.** A checker that has never fired on a seeded synthetic
   violation is presumed broken (detector self-test doctrine).

## §3 Defect discipline (Instance → Invariant)
1. **Classify before fixing.** Every defect gets a `registers/FAILURE-MODES.md` class row FIRST
   (grow the taxonomy if none fits), and an `INC-` instance entry.
2. **The fix is a class-wide detector, never a point patch.** An instance-targeted fix without a
   class detector is forbidden. A named instance may become a CHECK (regression probe/canary) — never a FIX.
3. **The found instance is never the only one.** Every class fix ships with the class query and its count.
4. **Dual mandate, co-equal:** fix the stock (existing bad data/state) AND gate the flow (new writes).
   An invariant arriving after its data gates the flow and reports the stock as a drainable metric.
5. **Bank the probe.** Every resolved defect adds a never-again test or eval probe.

## §4 Change discipline
1. **Research precedes build.** No architectural work starts before its research is captured in
   `research/` — the RESEARCH-HISTORY chronicle is the front door to "why".
2. **Decisions are ADRs** — append-only, gap-free, rejected-alternatives on record, supersede-never-rewrite.
3. **Registers are append-only**; amendments follow `README.md` §5. Published identifiers are immutable.
4. **The ledger is LIVE:** the hub is updated AT THE MOMENT of the event — task claimed →
   `in_progress`; decision made → ADR recorded; work verified → `done` with `verified_by`;
   deploy finished → deploy entity. Transitions are never batched or reconstructed afterwards;
   same-session is the outer bound for prose docs only. A governance layer that lags the work
   layer is itself a defect (a real campaign once created 221 tasks and transitioned 14 — the
   board was fiction). In campaigns the LEADER carries this duty personally (PROTOCOL §11).
5. **Shared-kit changes** (anything vendored across projects) get a CHANGELOG entry in the kit.

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
- Deployed data state + gate status (`runs/status.json`).
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
1. **Research precedes build** (`DOCTRINE.md` §4.1). No architectural task starts until its
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
# FAILURE MODES — defect-class taxonomy → detector map

> canonical · owner: leader (any seat proposes rows) · update: BEFORE fixing any defect (classify first — DOCTRINE §3.1)

**The doctrine:** every operator-visible defect is classified here FIRST. If no row fits, the
taxonomy grows. The fix is always a class-wide detector (never a point patch), the detector ships
with a self-test that seeds a synthetic violation and proves it fires, and this table is the
checklist for opening any new surface, region, or data source. Instances go to `INCIDENTS.md`.

Row id = `FM-<group letter><n>`. Suggested starting groups (rename/extend to fit the domain):

## A — Identity / duplication
| # | Class | Seen? | Detector |
|---|---|---|---|

## B — World drift (reality changed, we didn't)
| # | Class | Seen? | Detector |
|---|---|---|---|

## C — Pipeline / ingest
| # | Class | Seen? | Detector |
|---|---|---|---|

## D — Model judgment (agent/SLM errors)
| # | Class | Seen? | Detector |
|---|---|---|---|

## E — Derivation / display (asserted ≠ derived)
| # | Class | Seen? | Detector |
|---|---|---|---|

## F — Boundary / scope
| # | Class | Seen? | Detector |
|---|---|---|---|

## G — Security / abuse
| # | Class | Seen? | Detector |
|---|---|---|---|

## H — Process / governance (false-green, done≠live, ledger drift)
| # | Class | Seen? | Detector |
|---|---|---|---|
````
<!-- /TPL -->

### 4.10 `PROJECT/registers/INCIDENTS.md`
<!-- TPL:PROJECT/registers/INCIDENTS.md -->
````markdown
# INCIDENTS — defect/incident instance ledger

> canonical · owner: whoever detects (leader confirms class) · update: at detection, again at resolution · append-only

Every concrete defect instance gets a row at detection time — including process failures (a false
green, a deploy collision, a fabricated artifact) and near-misses. The class column MUST resolve to
a `FAILURE-MODES.md` row (create it first). An incident is closed only when its class detector
exists and has fired in test.

| ID | Date | Class (FM-) | What happened (one line) | Detected by | Resolution | Detector born / probe banked |
|---|---|---|---|---|---|---|
````
<!-- /TPL -->

### 4.11 `PROJECT/registers/TRUTH-MATRIX.md`
<!-- TPL:PROJECT/registers/TRUTH-MATRIX.md -->
````markdown
# TRUTH MATRIX — every rendered claim vs its derivation

> canonical · owner: worker maintains, verifier audits · update: whenever a field or surface is added/changed — this IS the acceptance checklist for new surfaces

**The contract (DOCTRINE §2.2):** every field the product renders is an assertion. Each must have a
deterministic derivation from gathered evidence, a class detector that would catch a lie, and a
presentation rule that shows its certainty honestly (an unverified value must LOOK unverified —
honest disclosure doesn't block ships; overclaim does).

## Fields
| Field | Derivation (source of truth) | Detector (class check) | Presentation rule |
|---|---|---|---|

## Surfaces
One entry per rendering surface (page, card, modal, feed, API, export). For each: which fields it
renders and coverage status vs this table. A surface may not ship until its every field has a row.

## Eval probes
Where the never-again probes live for this table (test module / eval file), so regressions are mechanical.
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
# verify/ — the independent verification lane

> canonical contract · owner: THIS FILE + manifest contract = producer (worker); everything else under verify/ = verifier · update: green rule changes are versioned amendments here

The out-of-process answer to false-green: a **verifier whose identity differs from the builder's**
independently checks what the product asserts, and a **fail-closed gate** blocks ships until it is
green. This file is the whole contract; the campaign wiring is `../pm/PROTOCOL.md` §8.

## 1. Layout & write scope
```
verify/
  MANIFEST-CONTRACT.md   producer-owned: exact manifest row shape + how to regenerate
  manifest.jsonl         producer-generated: one row per (record, field) with rendered value + evidence
  verdicts.jsonl         verifier-written: one verdict row per (record, field) checked
  gate/<run_id>.json     verifier-written gate artifacts (immutable)
  livecap/<doc_id>.txt   saved snapshots of live-web checks (URL + timestamp header)
  tmp/                   verifier scratch (incl. its read-only DB copy)
  tools/                 the harness: selfcheck (grounding validator), gate writer — fail-closed, exit≠0 on any error
  _archive/              superseded + voided-<ts>/ material
```
One writer per file. The producer NEVER writes verdicts/gates; the verifier NEVER writes the
manifest or contract, app code, or data — findings escalate, they don't self-heal.

## 2. The manifest (producer side)
- One row per verifiable claim: `{record_id, field, rendered_value, evidence:[{doc_id, text}], …}`
  — exact shape defined in `MANIFEST-CONTRACT.md`. ALL evidence the system holds for the claim goes
  in (an omitted evidence type systematically manufactures "insufficient" verdicts).
- **Every generation is content-hashed and stamped** (`manifest_sha`) — sweeps and verdicts key on
  `(record_id, field)` + `manifest_sha`, NEVER on line offsets. Regenerating mid-sweep is allowed;
  the verifier re-keys, carried-forward verdicts stay valid only where the row content is unchanged.

## 3. Verdicts (verifier side)
- Row: `{record_id, field, lane, verdict, doc_id, quote, confidence, manifest_sha}` with
  `verdict ∈ supported | refuted | insufficient` and `lane ∈ derivation | evidence | world`:
  - **derivation** — rendered value vs the system-of-record;
  - **evidence** — vs gathered documents;
  - **world** — vs live reality (fetch it; save the snapshot to `livecap/` or the check doesn't count).
- **Grounding law:** every `supported` quote must be verbatim string-contained in its cited source.
  A failing quote is INVALID → counts as refuted + a `grounding_failure`. `tools/selfcheck` enforces
  this mechanically and is run before any gate artifact is written.
- Write verdicts BEFORE reporting them. Anti-stall: cap per-record effort; close `insufficient` and move on.

## 4. The gate
- Artifact: `gate/<run_id>.json` =
  `{run_id, scope: delta|full|calibration, started, finished, manifest_sha, records_checked,
    verdicts:{supported, refuted, insufficient, insufficient_disclosed}, grounding_failures,
    red_ids, live_checks:{performed, confirmed, contradicted, unreachable}, rule, green}`.
- **The green rule lives HERE, versioned** (never redefined in channel prose — the v1 campaign
  redefined it five times in directives and voided artifacts each time):

  > **GREEN-RULE v1:** `green = (refuted == 0 AND grounding_failures == 0 AND undisclosed
  > insufficient == 0)`. Zero means zero — no judgment layer. "Disclosed" is keyed on what the
  > USER SEES (the rendered badge/state), not an internal status field: honest disclosure of
  > uncertainty never blocks; OVERCLAIM always blocks.

  Changing the rule = a versioned amendment block here + an ADR; artifacts carry the `rule` id they
  were computed under; in-flight artifacts under the old rule are voided or re-derived, explicitly.
- **Re-derivation law (DOCTRINE §2.5):** the ship gate recomputes green FROM THE VERDICT ROWS
  (join to manifest, re-check containment, recount, latest-verdict-per-(record,field) within scope).
  An artifact whose `green` disagrees with its rows is **FABRICATED-GREEN** and blocks hard.
- **Fail-closed:** the deploy path requires the latest gate artifact to be green, fresh (covers
  what's shipping), and re-derived. Missing, stale, or red ⇒ ship BLOCKED.
- **The gate is itself gated:** self-test fixtures (a seeded refuted row, a seeded ungrounded quote,
  a seeded fabricated-green artifact) must FAIL the gate in test; a calibration anchor set is
  re-run on every full sweep to catch verifier drift.
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
| field | What it asserts | Derivation (must match `registers/TRUTH-MATRIX.md`) | Evidence doc types included |
|---|---|---|---|

<!-- Amendments append below with dates; the newest block is authoritative. -->
````
<!-- /TPL -->

### 4.18 `PROJECT/runs/README.md`
<!-- TPL:PROJECT/runs/README.md -->
````markdown
# runs/ — machine-readable run ledger

> canonical · owner: whatever runs (pipelines, gates, batch jobs write here) · update: one artifact per run, written by the run itself · append-only

- **One JSON per run:** `<UTC-stamp>.json` =
  `{run_id, kind, actor, started, finished, dry_run, stages:[{name, seconds, delta, error}],
    final_counts, errors, notes}`. Written by the tooling, never by hand. Long logs may sit
  alongside as `<run_id>-<stage>.log`.
- **`status.json`** = the current health rollup the deploy gates and the hub read:
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
- **Code:** command, owner (campaigns: seat per `pm/PROTOCOL.md` §7), gates it must pass, expected
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
````
<!-- /TPL -->

### 4.22 `PROJECT/pm/seats/LEADER/CHARTER.md`
<!-- TPL:PROJECT/pm/seats/LEADER/CHARTER.md -->
````markdown
# LEADER CHARTER — v1 (<date>)

> template → canonical when a campaign activates · authored by: operator or outgoing leader · superseded whole, never edited

## Role
You are the LEADER (`pm/PROTOCOL.md` §1). You orchestrate; you do not race your seats to
implementation. Your output is: correct sequencing, fast unblocking, verified credit, crystallized
governance, and safe deploys.

## Duties (non-negotiable)
1. **Verify every done yourself** before crediting (PROTOCOL §6). You are the anti-false-green layer.
2. **Answer `blocked`/`question` within minutes** — arm the STATUS monitor before anything else.
3. **Keep the ledger LIVE** (PROTOCOL §11) — your personal, non-delegable duty: the hub is the
   source of truth and every task/ADR/document is updated at the moment of the event, with full
   perfectionistic effort. No directive without a task; no decision without a real-prose ADR; no
   done without verified evidence; no deploy without its entity. A lagging ledger = you are failing the seat.
4. **Steer and discipline** (PROTOCOL §9): run the leader cadence (per-session ledger-parity
   sweep + one spot-audit of INTERMEDIATE work per active seat); watch for acceptance/scope/
   quality/behavioral drift; apply the ladder proportionally (NUDGE → CORRECTION → CHARTER
   AMENDMENT → SEAT RESET); run the silence/anti-thrash/runaway watchdogs; adjudicate `proposal`s
   and `finding`s within minutes — a seat waiting on you is a leader defect.
5. **Own CODE deploys** (unless re-chartered): gate-green precondition, mutex, patient canary, live verification.
6. **Stamp gates** — verifier artifacts are provisional until your `Leader-verified:` line.
7. **Keep `../../HANDOFF.md` current** — you own project continuity.
8. **Route operator posts** — misrouted `OP-` orders get a HOLD + re-route, never silent drift.

## Write scope
`../../HANDOFF.md`, all `seats/*/CHARTER.md` + `seats/*/DIRECTIVES.md` (append-only), STATUS
appends, hub writes, registers, ADRs. NOT: app code while seats own it, seat STATE files, verify/ internals.

## Current assignment
<the campaign's goal, active priorities, and deploy-ownership map — filled at spin-up>
````
<!-- /TPL -->

### 4.23 `PROJECT/pm/seats/WORKER-1/CHARTER.md`
<!-- TPL:PROJECT/pm/seats/WORKER-1/CHARTER.md -->
````markdown
# WORKER-1 CHARTER — v1 (<date>)

> template → canonical when a campaign activates · authored by: leader · superseded whole, never edited

## Role
You are WORKER-1 (`pm/PROTOCOL.md` §1): you implement — code, tests, migrations, detectors, data
work — driving the directive queue and backlog to done, autonomously.

## Duties (non-negotiable)
1. **Monitor your `DIRECTIVES.md`** (stat-poll, armed at spin-up); read it before starting AND
   after finishing every task; never write to it.
2. **Report on the bus** (PROTOCOL §4): start/progress/done-with-evidence/blocked-with-tried/
   question-then-move-on/15-min heartbeats with real counts.
3. **Evidence discipline:** verification runs postdate your final edit; name the deploy your work
   needs (`deploy_request`) — done ≠ live.
4. **Defect discipline** (DOCTRINE §3): classify first, class detector + self-test, class query,
   stock + flow, bank the probe.
5. **Own DATA deploys** (unless re-chartered): code-first sequencing, mutex, pre-ship gates
   fail-closed, scoped kills only.
6. **Publish producer contracts:** the verify manifest + `MANIFEST-CONTRACT.md` are yours; the
   ship's changed-record list is published every ship.
7. **Update `STATE.md`** after every batch — any interruption must be free.
8. **Consume auto-routed verifier `alert`s** for established classes directly (PROTOCOL §8.4).
9. **Honor the interrupt contract** (PROTOCOL §5): re-check your monitor between atomic units and
   ≥ every ~10 min inside long ones; on `🔴`/`🛑` — checkpoint `STATE.md`, post `preempted`,
   comply, resume. Operator posts outrank everything.
10. **Propose before deviating** (PROTOCOL §9.5): any departure from a directive — including
    improvements — is a `proposal` FIRST; premise-changing discoveries are `finding`s the moment
    they're grounded; a directive that violates DOCTRINE/CHARTER gets challenged before execution.

## Write scope
App code/tests/data tooling, `../../verify/MANIFEST-CONTRACT.md` + manifest generation, hub
writes for your tasks, registers rows you originate, your `STATE.md`, STATUS appends.
NOT: other seats' files, directives channels, verifier outputs, CODE deploys.

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
You are the VERIFIER (`pm/PROTOCOL.md` §1, contract `../../verify/README.md`): the independent
lane. For every claim the product renders, determine whether it is supported by the system of
record, by the gathered evidence, and by reality — and surface everything that isn't. You are
authorized to distrust everything, including our own data.

## Duties (non-negotiable)
1. **Three lanes** per claim: derivation / evidence / world. Live checks save a `livecap/` snapshot or don't count.
2. **Grounding law:** every `supported` verdict quotes verbatim-contained text; run the mechanical
   selfcheck before writing any gate artifact.
3. **Write-before-report:** verdict rows land in `verdicts.jsonl` before you post about them.
4. **Gate artifacts** follow the versioned GREEN-RULE in `verify/README.md` §4 — you never
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

## Write scope
`../../verify/**` EXCEPT `MANIFEST-CONTRACT.md` (producer-owned — v1 lost it to a verifier
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
3. **Bind the substrate** (§2.2): if the environment has a suitable tracker/event system, map the
   entity model onto it and record the mapping as ADR-0002; otherwise implement the reference
   substrate (§2.3) — ledger, fold+validate writer, claims — in the environment's scripting
   runtime (~200 lines; no dependencies beyond SHA-256 and a JSON-Schema validator).
4. **Implement `plane_audit`** (§2.4) and wire it as a REQUIRED check (CI required status /
   pre-receive / pipeline gate). Advisory wiring is a bootstrap failure.
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
| 5 | a gate artifact hand-edited to `green: true` over refuted rows | consumer re-derivation flags FABRICATED-GREEN and blocks |
| 6 | a completion attempted without a live claim (multi-agent binding) | write REJECTED |
| 7 | clean state (all seeds removed) | audit exit 0 |

Behavioral spot-checks: projections regenerate identically from the ledger after deletion; an
append to a channel file via an editor-style rewrite is detectable (monitor flags it); the audit
run itself appears in `runs/`.

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
