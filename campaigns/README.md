# Campaigns — the robust agent prompts that maintain, improve & augment the hub

The hub and the plane are the *nouns* of this system; **campaigns are the verbs**. This directory
holds the battle-tested agent-prompt playbooks for running multi-agent (or single-agent) work over a
project's hub without losing state or shipping false-green. They were distilled from real campaigns
that audited, hardened, backfilled, and extended a working multi-project estate.

Each file is a **prompt you hand to an agent** (or a fan-out of agents), written to be pasted or
adapted directly. They assume the hub + plane in this scaffold, but the method is harness-neutral —
run them with one agent, a hand-rolled fan-out, or an orchestration tool if you have one.

| File | Verb | Use it to… |
|---|---|---|
| `00-orchestration-method.md` | (engine) | run ANY of the below well: proportionate fan-out, persistence, and boundary-triggered verification. Read first. |
| `maintain-audit-reconcile.md` | MAINTAIN | reconcile code ↔ hub ↔ live ↔ docs; catch drift, stale claims, false-green; regenerate the state anchor. |
| `improve-moe-review.md` | IMPROVE | a multi-expert review→verify→committed-report pass over a codebase (the flagship: correctness/security/architecture/truth/research experts + adversarial closer). |
| `augment-hub.md` | AUGMENT | add a new entity type + tab to the hub, or backfill governance/structure across repos, the same way the base types were built. |
| `feature-buildout.md` | BUILD | drive real feature work off the board with the DISCOVER→CLAIM→IMPLEMENT→RECORD→VERIFY loop; includes the leader / worker / verifier roles for long multi-session arcs. |
| `verification-closer.md` | VERIFY | dispatch one fresh read-only closer at a meaningful boundary; receive a verdict and end the verifier session. |

## The three non-negotiables (why these prompts are "robust")
1. **Evidence is honest and proportional.** Routine work does not automatically spawn another agent
   or run the full selftest ladder. Releases and risky boundaries receive independent verification
   against the real mount — there is no unit battery to hide behind.
2. **Independent means separate.** A boundary closer starts fresh, tries to refute the claim, reports
   one verdict, and exits without fixing its own findings.
3. **Never lose or clobber concurrent work.** Targeted commits only; another session's uncommitted
   files are read, never staged. Persist results as you go so a killed run loses nothing.

## Placeholders
`{{PROJECT_KEY}}`, `{{BRAND}}`, `{{LIVE_URL}}`, `{{DEPLOY_CMD}}`, `{{REPO_PATH}}` — substitute for your
environment (or let `init.sh` do the first three). Nothing here names any specific person, host, or
project by design; keep it that way (`tools/scrub_check.sh` enforces it).
