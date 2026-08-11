# IMPROVE — multi-expert review → verify → committed report

The flagship campaign: a mixture-of-experts audit of a codebase (or a slice of one), where each expert
is an independent perspective, findings are adversarially verified, and a closer writes a durable report.
This is how you take an existing thing to a higher standard *honestly* — every finding is grounded and
re-checked before it's written down. Use it on one repo or fan it across many (one panel per repo).

Read `00-orchestration-method.md` first. Substitute `{{REPO_PATH}}`, `{{LIVE_URL}}`.

---

## Stage 1 — the expert panel (one agent per expert, parallel)

Preamble given to EVERY expert agent:

> You are auditing a REAL production codebase at `{{REPO_PATH}}`. STRICT read-only — use read/search
> tools and the web; do NOT edit, run, migrate, deploy, or git-mutate anything. Exclude from scope:
> virtualenvs, `node_modules`, build/dist output, vendored bundles, minified files, binary assets —
> audit the project's OWN source. Read enough to be RIGHT, not fast: open the load-bearing files fully,
> trace the real call paths, and VERIFY a claim before reporting it. Every finding MUST cite a
> `file:line` you actually read plus a concrete failure scenario (inputs/state → wrong result), not a
> vague worry. Prefer a few high-confidence findings over many speculative ones. Return structured
> findings: `{title, severity(critical|high|med|low), file, line, description, evidence,
> failure_scenario, verify_hint, fix_sketch}`.

The five experts (each appended to the preamble as "YOUR FOCUS"):

- **CORRECTNESS / BUGS** — logic errors, off-by-one, unhandled errors/exceptions, race conditions &
  concurrency (locks, leases, async, event-store appends), data-integrity (migrations, serialization,
  idempotency, money/count math), wrong edge-case handling, silent failures. Find defects that produce
  WRONG output or crashes on real inputs.
- **SECURITY** — authn/authz gaps, injection (SQL/shell/template), SSRF, unsafe deserialization,
  secrets in code, PII/internal-info on public routes, CSRF/clickjacking, permissive
  CORS/host-allowlist/DEBUG-in-prod, path traversal, `shell=True` on user input, token handling. Judge
  the live posture, not theoretical worries.
- **ARCHITECTURE & DEPENDENCY-HEALTH** — structural coupling, dead/duplicated code, leaky abstractions,
  missing tests around risky code, performance hot paths (N+1, unbounded loops, sync-in-request). Read
  the dependency manifests and assess pinned-vs-floating, outdated/deprecated/EOL packages, anything
  that smells vulnerable — cite the manifest line.
- **TRUTH-vs-LIVE (is it honest)** — reconcile what the CODE does against what the repo CLAIMS. Compare
  the plane docs, hub state, README, and code COMMENTS against actual behavior; check the committed
  HEAD against the live build (fetch `{{LIVE_URL}}` for the `<meta name="build">` stamp) for
  deployed==HEAD coherence; flag false-green residue, stale/aspirational docs presented as done,
  comments describing behavior the code no longer has, and TODO/FIXME/HACK debt.
- **EXTERNAL RESEARCH (use web search/fetch)** — is this stack current & best-of-breed for its domain?
  Read the manifest, pick the 3–6 most load-bearing dependencies, and research each: latest stable
  version, known CVEs at the pinned version, deprecation/EOL, and whether a materially better approach
  now exists. Assess the architecture against current best practice for its domain. Cite source URLs.
  Distinguish "genuinely worth changing" from "fine as-is".

For a small repo, collapse to one **survey** expert (correctness + security + honesty in one pass,
proportional to size). For a medium repo, run correctness + security + truth.

## Stage 2 — the closer (one per repo; adversarial verify + write + commit)

> You are the CLOSER + adversarial verifier for `{{REPO_PATH}}`. STRICT read-only on code; the ONLY
> write is the report. The expert panel produced these raw findings: `<JSON>`.
>
> 1. **Merge & dedupe** across experts. Drop duplicates and anything that is not a real, grounded defect.
> 2. **Adversarially verify** the top findings (up to ~12, most-severe first): re-open the cited
>    `file:line` yourself and try to REFUTE each. Mark **CONFIRMED** (you re-read the exact code and the
>    logic holds) or **PLAUSIBLE** (real-looking, not fully grounded). Discard what you can refute;
>    count refutations. Check for existing mitigations before confirming.
> 3. Write a meticulous **`AUDIT-<date>.md`**: repo headline + a justified health score (0–100); a
>    severity-ranked table of CONFIRMED/PLAUSIBLE findings (title · sev · `file:line` · failure scenario
>    · fix sketch); a stack-currency section (latest-vs-pinned, CVEs, better-tool notes with source
>    URLs); a truth/coherence section (code-vs-live-vs-docs); and a short "top risks / recommended next
>    actions" list. This is a durable record — honest and concrete.
> 4. Commit **only that one file** (`git add AUDIT-<date>.md` + commit). NEVER `git add -A`/stash/
>    checkout — the repo may hold another session's uncommitted work you must not stage. If the repo is
>    a separate world / holds a live session, write the report OUTSIDE it and commit nothing inside.
> Return a structured summary (confirmed[] with verdicts, top_risks, stack_currency one-liner,
> report_path, commit_sha).

## Stage 3 — estate rollup (only when auditing many repos)

> You are the estate synthesizer. Given every closer's summary, write the machine-wide rollup: an
> executive summary (overall health + the single most important thing to know); a ranked table of every
> repo; **cross-cutting themes** (a bug class, a shared vulnerable dep, a coherence gap that recurs —
> worth one systemic fix); the estate TOP-20 findings severity-ranked (repo + `file` + one-line failure
> + fix direction); a stack-currency roundup; and a remediation WAVE plan (fix-first / batchable /
> needs-human-decision), explicitly naming any separate-world or live-session repos to leave alone.

## From findings to fixes

This campaign is **report-only** by design. Remediation is a SEPARATE campaign: turn each CONFIRMED
finding into a hub task (with a `verification_command` if you run `strict`), then drive them with
`feature-buildout.md`, re-verifying before each fix. Never fix in the same pass that finds — the
finder and the fixer having different eyes is a feature.
