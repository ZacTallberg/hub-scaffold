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
