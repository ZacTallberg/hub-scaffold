# The Standing Conformance Scanner — spec

A pattern, not a runnable script: a small stdlib-only scanner your org implements once, which
mechanically scores **every project** against the org standard and regenerates a single matrix
document on a schedule. It is the portfolio-level counterpart of the deploy contract: the deploy
canary catches one project drifting from its blessed build; the conformance scanner catches the
whole fleet drifting from the standard.

## Why it exists

- **Standards decay silently.** A project is governed the day it's created and ungoverned six
  months later; nobody notices because nobody re-checks. Mechanical rescan makes decay visible
  the week it happens.
- **Self-reported status is FALSE-GREEN at fleet scale.** "All projects follow the template" is
  a claim; a generated matrix with per-check evidence is a fact.
- **It tells you where to point effort.** Judgment-layer quality (tests, UX, docs currency) is
  deliberately OUT of scope — that's what per-project improvement campaigns are for. The scanner
  answers only "which projects are missing the mechanical baseline, and on which dimension."

## Shape

1. A **project registry** the scanner iterates — ideally read from your org's existing project
   index (a registry service, a monorepo manifest, an org's repo list via API), not a hand-kept
   array. Each entry: name, working dir / repo, live URL (or none for libraries/internal tools).
2. A **check catalogue** (below), each check returning `PASS` / `WARN` / `FAIL` / `NA`
   ("not applicable" — excluded from the denominator, so a CLI tool isn't punished for having no
   live URL).
3. A **generated matrix doc** (markdown table: one row per project, one column per check, a
   `score/max` column, sorted worst-last or best-first) plus a machine-readable JSON twin.
   Both carry a "GENERATED — do not hand-edit; re-run the scanner" banner and a timestamp.
   A "Failing details" section lists every FAIL/WARN with its one-line detail so the reader
   never has to re-run the scan just to know what a red cell means.
4. A **schedule**: cron / CI job reruns the scan (daily or weekly is plenty). The scanner
   compares the new JSON to the previous run and fires the org alert channel on **regression**
   (any project whose score dropped, or any PASS→FAIL transition). New projects appearing with
   low scores are expected and not an alert — regressions are the signal.

## Check catalogue

Three dimensions. Adapt names/paths to your org; keep every check *mechanical* — a check an
implementer could argue about belongs in a review, not a scanner.

### A. Governed — the project carries its own operating rules

| Check | PASS when | Notes |
|---|---|---|
| `exists` | project directory / repo present | FAIL short-circuits the row |
| `git` | it is a git repo | everything downstream assumes commits |
| `agent_rules` | agent governance file present (e.g. `CLAUDE.md` / `AGENTS.md`) | see governance/ templates |
| `settings` | harness settings committed (e.g. `.claude/settings.json`) | hooks/permissions travel with the repo |
| `plane` | project-plane tree present (`PROJECT/` or at minimum a decision log dir) | decisions are append-only records |
| `memory_store` | per-project agent memory store exists | only if your org runs one; else NA |

### B. Truthful — what the project claims matches what is observable

| Check | PASS when | Notes |
|---|---|---|
| `deploy_wrapper` | a contract-compliant `deploy.sh` exists | NA if no live URL |
| `live` | front-door URL returns HTTP 200 | fetched with a browser-like UA (some edges filter bot UAs) |
| `build_meta` | live page serves `<meta name="build" content="build-<sha>">` | no meta = canary-blind, FAIL |
| `coherence` | live SHA == local HEAD short SHA | WARN on mismatch (undeployed commits or unstamped deploy), not FAIL |
| `blessed` | a blessed record exists for the project on the canary host | i.e. the standing canary is watching it; probe gracefully — an unreachable canary host makes this NA, not FAIL |
| `registered` | project appears in the org's project registry | NA for projects with no public surface, if your registry only tracks deployed ones |
| `done_verified` | done-claims are server-verified, not self-attested | if the org runs a task hub: the hub's write path demands verification evidence (e.g. a required verification-command field). NA otherwise |

### C. Clean — the repo contains only what it should

| Check | PASS when | Notes |
|---|---|---|
| `no_secrets_tracked` | no tracked file matches secret shapes: `(^|/)(\.env|creds[^/]*|id_rsa[^/]*|.*\.pem)$`, excluding sanctioned `*.example` files | mirror pre-receive-gate.sh's shapes so the two layers agree |
| `no_junk_tracked` | no tracked top-level `*.log` / `*.sqlite3` (or your org's build-artifact shapes) | WARN, not FAIL — junk is untidy, not dangerous |
| `drift` | `git status --porcelain` empty | WARN up to a small threshold (~15 dirty files), FAIL beyond — a giant uncommitted pile is unshippable work |

## Implementation notes

- **Stdlib only, graceful probes.** Every network/remote check (live fetch, canary-host probe)
  must degrade to NA on unreachability rather than crashing the scan or faking a FAIL.
- **Scoring**: `score = PASS count`, `max = non-NA count`; sort rows by ratio. Never weight —
  weights invite gaming; a red cell is a red cell.
- **The matrix is generated output, never edited.** Hand-editing the matrix is itself
  false-green; the banner says so.
- **Keep checks cheap** (a full fleet scan should take seconds-to-a-minute) so nobody is
  tempted to skip the schedule.
- **Regression alerting is the standing part.** A one-shot scan is an audit; the pattern is a
  scanner + schedule + diff-vs-last-run + alert. Wire the alert through the same `ALERT_CMD`
  wrapper the standing canary uses.
- **Adding a check** is how the org ratchets its standard: introduce it, let the matrix go red,
  burn the reds down, and the new bar is now enforced forever at zero ongoing cost.
