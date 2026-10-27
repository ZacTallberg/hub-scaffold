# Vendoring one Django app kit into several apps on one database

A reusable Django app ("kit") — a sign-in gate, an error recorder, a request inbox — is often
copied into several projects rather than installed as a package, so each project can adapt it.
When those projects share ONE database and the kit keeps ONE app label across all of them, the
copy step has two traps that fail silently with a green deploy.

## Trap 1 — migration names collide, and the second app creates no tables

Django records an applied migration by `(app_label, name)`. The first app to migrate records
`kit_label.0001_initial`; the second app ships a migration with the same label and name, Django
sees it as already applied, and creates NONE of its tables. The deploy is green, the migrate step
prints nothing alarming, and the first request that touches the kit fails.

**Rule:** the copy step renames every migration FILE per destination app
(`0001_initial.py` → `0001_budget_app_initial.py`) and rewrites every reference to the old module
names — the `dependencies` of the next migration, and anything else that names a migration file —
before any other textual rewrite runs, so a name can never be half-rewritten.

In this repository `app-kit/tools/kits.py add` does it for every kit whose `KIT.json` declares
`"retarget_migrations": true` (the gate kit): the slug is `--slug`, else the one the app recorded
before, else the app directory's name; the renames and the rewritten bytes are recorded in the
app's `.app-kit.json`, so `status` judges the copy against what was actually written. Declare it
only for a kit whose TABLES are already per-app: renaming the migrations of a kit that keeps one
shared table makes the second app's `migrate` try to create a table that exists.

## Trap 2 — explicit index names collide

Django derives most index names, but an explicitly named `models.Index(name=...)` is a literal,
and index names must be unique per database. **Rule:** retarget explicit index names by a short
per-app prefix (e.g. the slug's initials, capped so the whole name stays within Django's 30
character limit).

## What the copy step must never do

An app that was vendored BEFORE the rename holds the old files; if the new files are added beside
them, two migrations share each number. That state is not the tool's to resolve by deleting
somebody's migration history. Report it for a person, and point at the `django_migrations` rows
so they can see whether the app already migrated under the old names.
`kits.py add` keeps a re-vendored copy under the names it was first vendored with and refuses
a different `--slug` for it, rather than renaming migrations a database may already record.

## Updating a vendored copy is a three-way merge, never an overwrite

A vendored copy is edited in place -- that is why it was copied -- so "update to the current kit"
by overwriting loses the edits, and refusing every edited copy (`add` without `--force`) leaves
it behind forever. The update is per file, between the kit as the app RECORDED it (base), the kit
now (theirs) and the app's file (ours): untouched -> take theirs; the kit did not change it ->
keep ours; both changed -> `git merge-file`, clean or a conflict; new in the kit -> add (only
inside what the kit installs); dropped by the kit -> reported, never deleted. Five rules, each
forced by a real record on the system this was lifted from:

- **Verify the base, file by file.** A copy re-vendored from another commit under an older record
  merges against the wrong base unless each base file is checked against the record's own sha; a
  file that does not match is looked up by that EXACT sha in the kit's history, and an
  unverifiable base refuses the whole update.
- **All or nothing.** One conflict writes nothing; the material (ours, base, theirs, the marked
  merge) is left beside the app for whoever resolves it.
- **The record keeps the KIT's bytes, never the app's**, so a kept or merged edit is still an
  edit on the next update. Recording the app's bytes erases the edit's history: the next update
  sees an "untouched" file and overwrites it.
- **Retarget the base and theirs the way the copy step did** (renamed migrations, rewritten
  references) before comparing, or every retargeted file reads as edited.
- **Write in the app's line endings** (and compare normalised), so the diff is the kit's change,
  not a whole-file CRLF churn; a record written over a CRLF tree must still match.

`kits.py update <kit> <app> [--dry-run]` is the implementation.

## A lane that files those updates must not add noise

When a hub files "bring app X to kit release N" tasks automatically on every release (one task
per stale app, proven by a live probe of the app's kit surface), the lane meets every failure a
background filer can meet, and on the origin system it met each one within a day:

- **File off the request.** A trigger that files tasks for many apps inside one request outlives
  the proxy's timeout; answer 202, file on a worker, and record progress the trigger reads back.
- **Let the release's own pipeline measure first**; a backstop that launches its own probe the
  moment it sees a release races it and queues a duplicate.
- **Read before you launch, and fail closed on an unread read.** Launch a live proof only when the
  app HAS the surface to prove (its CI defines the probe job, its record carries the kit's files);
  an unreadable CI file or record is "could not read -- nothing launched", never "go".
- **"Not provable yet" is not "failed".** A probe that could not run (no probe job, the probe
  identity not admitted, the release not landed) is one named checkpoint, retried later -- never a
  fail row and never a problem. A cancelled or skipped job is no verdict; only the probe job's own
  terminal result is. A copy that ships only the part of the kit without the probed surface is
  proven by its record (the deployed record names the target release), not parked forever.
- **Withdraw your own false verdicts with one marker**, never by rewriting a receipt (receipts are
  restored on every write) -- and only rows the lane itself wrote.
- **The lane's notes are lifecycle rows**, so a close that reads "the task's own word" never quotes
  the lane's "not provable yet" back as the work being unfinished.
- **Never file over live work.** A task NAMED for the app, a live console in the app's repository,
  or a recent push that touched the kit's own files by someone not on a sync task is live work:
  defer and say so once on that task. A hand-vendored copy is behind only when it LACKS something
  (a file behind, diverged or missing), not because it differs.
- **Add no write load.** An open sync task is never rewritten per release (its recipe reads the
  current kit when it runs); cap open sync tasks; a busy write lock is retried with back-off and
  then deferred to the next release, never an error; a version conflict from a concurrent write
  is re-read and retried a bounded number of times.

## Proof

Run the real operation once: copy the kit into a scratch clone of a destination app, let that
app's own Django load the migration graph and migrate a scratch database, and read back the
created tables, the index names, and the `django_migrations` rows. A copy that "looks right" is
not evidence; the table list is.
