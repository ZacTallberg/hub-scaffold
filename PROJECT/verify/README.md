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
