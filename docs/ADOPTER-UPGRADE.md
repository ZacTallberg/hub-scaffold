# Upgrade an existing adopter

The upgrader advances four canonical units together so engine, Django adapter, schemas, and their
operating contract cannot silently diverge. It writes only their configured destinations and the
manifest. It never touches `PROJECT/project.json`, `PROJECT/seed.json`, `PROJECT/state.json`,
`PROJECT/.hub/`, CHARTER/HANDOFF, ADRs, research, registers, or adopter-only files, and it never
creates or runs tests. Project laws appended under `DOCTRINE.md` §6 survive contract upgrades.

Create a tracked `hub-scaffold-adoption.json` at the adopter repository root:

```json
{
  "schema_version": 1,
  "units": {
    "hub_core": {"destination": "hub_core"},
    "django_hub": {"destination": "hub"},
    "project_schema": {"destination": "PROJECT/schema"},
    "project_contracts": {"destination": "PROJECT"}
  }
}
```

Destinations are relative to the manifest and may be changed for another package layout. From the
`hub-scaffold` checkout, preview or apply the whole-unit upsert:

```bash
python tools/upgrade_adopter.py ../my-project/hub-scaffold-adoption.json --dry-run
python tools/upgrade_adopter.py ../my-project/hub-scaffold-adoption.json
```

The first run refuses different files already occupying canonical paths because it has no ownership
record. Review them, move genuine extensions to distinct files, then use `--overwrite-managed` once
to establish ownership. Later upgrades update unchanged managed files automatically and refuse local
edits to canonical regions unless that explicit option is supplied. Adopter extras and upstream-
retired files that were never manifest-owned are not deleted. A formerly managed file removed
upstream is pruned only while its current hash still matches the last installed hash; adopter drift
refuses the upgrade instead. The writes, safe prunes, and provenance manifest apply as one
transaction, and a write failure restores the exact pre-upgrade file bytes.
Changing a configured destination never deletes the former destination; relocate or retire that old
tree explicitly after review.

On success, `adopted_from` records the scaffold repository, commit, dirty-state truth, aggregate
tree hash, and source and installed SHA-256 hashes for every managed file. Commit that manifest with
the adopted units. Then use the adopter's actual mounted Hub operation as the receipt; do not invent
an upgrade test.
