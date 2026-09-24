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

## Proof

Run the real operation once: copy the kit into a scratch clone of a destination app, let that
app's own Django load the migration graph and migrate a scratch database, and read back the
created tables, the index names, and the `django_migrations` rows. A copy that "looks right" is
not evidence; the table list is.
