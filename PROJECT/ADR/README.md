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
