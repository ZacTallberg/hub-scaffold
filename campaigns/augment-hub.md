# AUGMENT — extend the hub (new entity type + tab) or backfill structure across repos

Grow the system the same disciplined way its base was built. Two common augmentations:
**A. add a new first-class entity type + hub tab** (e.g. Findings, Lessons, Decisions-log); and
**B. backfill governance/structure across many repos at once**. Both are AUGMENT, not IMPROVE — you're
adding capacity, not fixing defects.

Read `00-orchestration-method.md` first.

---

## A. Add a new entity type + tab to the hub

The hub is extensible by design: a new type is a schema + a write path + a render tab. This is exactly
how the base types (task/adr/feature/gap/…) were built, so an added type is a first-class citizen, not
a bolt-on. Give an agent this:

> Add a new hub entity type `<TYPE>` (e.g. `lesson`) end-to-end — schema, write-API support, and a hub
> tab — matching the EXISTING types exactly (do not invent a parallel convention). Steps, each verified:
> 1. **Schema** — add `PROJECT/schema/<type>.schema.json` modeled on a sibling (e.g. `note.schema.json`):
>    its fields, required set, and any `if/then` honesty rules (the rules that make a false claim fail
>    validation — e.g. a `done` entity requires evidence). Register it wherever the schema registry
>    enumerates types.
> 2. **Write path** — if the type needs more than the generic upsert, add a typed endpoint in the write
>    API following the existing decorator/validation/OCC/idempotency pattern; keep it token-gated.
> 3. **Tab** — add a `TABS` entry in the hub frontend with the type's key, label, columns, and row
>    projection, matching the existing table pattern (sortable/filterable). If the type needs a detail
>    view (a modal with the full record, not just a row), model it on the richest existing detail render
>    — show EVERYTHING the record holds, not a truncated summary.
> 4. **Seed + prove** — add a couple of genesis rows (or a management command), then boot the site and
>    confirm the new tab renders, the write path validates (including a rejection case), and the audit
>    stays green. Report the exact refusal/accept results you observed — do not claim it works unwired.
> Keep it OPTIONAL if it's not universal: gate it behind a setting or ship it as an add-on so the base
> stays minimal for projects that don't need it.

**Reusable-vs-specific test:** before adding a type, ask whether it belongs in the *generic* hub or
only in *this* project. Findings, Lessons, and a Decisions-log are generic (any serious project wants
them). A domain type (a catalog specific to what this one app does) belongs in that app only — add it
there, not to the shared base.

## B. Backfill governance / structure across many repos

When N repos need the same structural addition (a governance file, a settings profile, a plane
skeleton, a deploy wrapper), fan out one agent per repo. The prompt that kept this safe across a real
estate:

> Backfill `<the files>` into `{{REPO_PATH}}`. Follow EXACTLY, touch NOTHING else:
> 1. If the target file already EXISTS, do not rewrite it — only APPEND the one missing clause if it's
>    absent (and report "amended"); else report "left-as-is". Create it only if MISSING.
> 2. ADAPT to reality, don't template blindly: read the repo's README/structure and drop or adjust any
>    line that references something this repo doesn't have. Never invent facts.
> 3. Commit ONLY the file(s) you created/changed with a targeted `git add`. NEVER `git add -A`, stash,
>    or checkout — this repo may hold another session's uncommitted work you MUST NOT stage or disturb.
>    If it isn't a git repo or is a separate world, report skipped with the reason.
> 4. Verify with `git show --stat HEAD` that only your files are in the commit; report the SHA + file
>    list. If you changed nothing, make no commit.

Run these in parallel (one per repo), then read every agent's structured report and confirm the commit
list — don't trust "done" without the `git show --stat` evidence.

## The universal guardrails (both A and B)
- Match the existing convention; a new type/file that follows a different pattern is tech debt on day one.
- Prove it wired and working (booted, validated, rendered) — an added tab that shows nothing, or a
  write path that 500s, is worse than not adding it. Claimed-done ≠ done.
- Keep the base minimal: universal additions go in the shared core; project-specific ones stay local.
