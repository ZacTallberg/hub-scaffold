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
