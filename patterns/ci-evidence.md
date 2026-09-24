# CI evidence: make the worst CI failures visible, and keep what proves them

**Status: a pattern, not a running component.** The scaffold ships no CI ingest. An adopter that
forwards its CI system's events into the Hub (webhooks, a poller, a runner hook) wires this
themselves; until then nothing here runs. The rules below come from operating such a lane and
each one names the failure it prevents.

## 1. A pipeline that never starts is the worst CI failure — name it

A pipeline rejected at creation (invalid configuration, an include that no longer resolves, a
rule that matches nothing) has **zero jobs**. Every "which job failed?" reader then reports
"unknown job", or nothing, and the failure looks milder than a red test when it is worse:
nothing ran at all, on every push, until someone notices.

- When a failed pipeline has no jobs, **ask the CI system's own configuration check** (most
  expose a lint endpoint) and classify the answer: `invalid` (with the linter's messages),
  `valid` (then the rejection came from somewhere else — say so), or `unreadable` (the check
  itself could not be asked).
- Record it as a **critical** problem with its own code (e.g. `ci_config_rejected`), never
  folded into a generic job failure.

## 2. Keep the raw event body — bounded, rotated BEFORE the write

A parsed summary is an opinion about the event. When a classification turns out wrong, the only
way to re-derive it is the body that arrived.

- Append each raw body as one JSON line to a retention file with a hard bound (for example two
  generations of 4 MB). **Rotate before writing, never after**: a writer that rotates after a
  successful write stops rotating the moment the disk is full, because the write never succeeds
  again — the full disk becomes the state it cannot leave.
- Serve it through a read route restricted to members, and make that route **describe its own
  store** (generation sizes, record counts, oldest and newest timestamps, the last retention
  error) without exposing server paths. A reader that does not name its subject can report an
  empty store for hours while it is reading the wrong file.

## 3. Fail-soft is fine; fail-SILENT is not

Retention must never break ingest, so it is wrapped fail-soft. But a fail-soft path that records
nothing ships a dead feature that reads as "no events". Every swallowed retention error goes to
the operational error stream (`/hub/api/agent-error` or the in-process equivalent) with its
exception class — once per distinct failure, not once per event.

## 4. A line writer adds the newline; a line reader survives when it did not

An `append_line(path, text)` helper that does not append `"\n"` concatenates every record into one
unreadable line — it was observed as a single 862 KB line that parsed as "empty". Make the writer
add the newline itself, and make the reader robust to history it cannot re-write: decode each line
with `json.JSONDecoder().raw_decode` in a loop so concatenated records are recovered one by one.

## 5. A skipped schedule is invisible — detect it with two observations

Scheduled pipelines can silently stop (the schedule's owner lost access, a rule stopped matching):
the scheduler advances `next_run_at` whether or not it created a pipeline, and nothing goes red.

- Keep the previous reading of every active schedule. A slot was **skipped** when `next_run_at`
  moved **and** the schedule's last pipeline id did not. Count consecutive skips.
- A first reading alone never fires (there is nothing to compare). No cron parsing and no
  inference about what "should" have run.
- If the poller has no credential, report "cannot check" — never an all-clear.

## 6. Every read route declares its audience

A route added without an explicit audience declaration (open, member-only, leader-only) is the
one a later audit cannot classify. Declare it at the view, in the same change that adds the route,
and let the route audit fail on an undeclared one.
