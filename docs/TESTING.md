# Verification

Verification means exercising the real artifact your change touched and recording what actually
happened. This repo deliberately ships **no unit battery**: a suite is green whenever the repo is
healthy, whether or not your change works, so a battery proves the repo, never the change — and
once a battery exists, every completion learns to pay its price and to trust its green. The engine
that once lived in `hub_core/tests/` was removed on those grounds (upstream ruling, 2026-08-08),
and the write API refuses a bare suite runner as a task's `verification_command` for the same
reason.

What replaces it is not "no verification" — it is verification with the right subject:

## Three obligations

**1. A guard is proven by watching it fire.** When you write or change a guard, seed a real
positive and watch it go red, then confirm it stays quiet on a true negative — at the time you
write it, in the session. A one-directional proof does not count, and a guard nobody ever saw fire
is a vacuous guard. Leave no test file behind; record the two runs (command + output) as the
task's receipt. `tools/scrub_check.sh --selftest` is the standing example of the form: it seeds a
violation, proves the gate catches it, and proves boundary-safe text passes.

**2. A feature is proven against the real thing.** Boot the actual example app and drive the
actual surface — `tools/selftest.sh` step 5 does exactly this for the write path (real Django
process, real refusal ladder, real CSRF-mint/token-consume boundary). For your own change, the
receipt is a command whose subject is the artifact you touched: a probe against the running
mount, a CLI invocation of the tool you changed, a diff of generated output. Not a suite.

**3. The floor is compile-and-import.** `bash tools/check.sh` keeps the cheap floor on every
edit: the agnosticism scrub plus `compileall` over every python surface. It costs seconds and
catches the class a battery only ever caught incidentally.

## Levels

| Level | Use when | Mechanism |
|---|---|---|
| Judgment | Tiny, low-risk, directly inspectable change | Read the diff; record truthful evidence. No command is mandatory. |
| Fast sanity | Ordinary pending work | `bash tools/check.sh` — scrub + compile floor, selected from changed paths. |
| Focused proof | A behavior changed | The smallest command that exercises the changed artifact itself, run by you, receipt recorded. |
| Independent boundary verification | Release, security/auth, migration/destructive work, public API/schema compatibility, concurrency/process launch | One fresh read-only `verification-closer` against the real mount; `bash tools/selftest.sh` when the boundary justifies the full ladder. |

Independent closers are disposable. They receive the raw target and claim, return one
`PASS`/`FAIL`/`INCONCLUSIVE` verdict with evidence and gaps, and exit. They do not fix their own
findings or wait for more work. See [the prompt](../campaigns/verification-closer.md) and reusable
[$verification-closer skill](../skills/verification-closer/SKILL.md).

## The selftest ladder

```bash
bash tools/selftest.sh
```

Four cheap steps and one real one: agnosticism scrub (both directions), compile floor, doc
integrity, bootstrap integrity — then the step that matters, booting the example site and running
the write API's full refusal ladder in-process. Use it for releases and risky boundaries, not as
an every-edit ritual.

On Windows, run it under Git Bash:

```powershell
& "$env:ProgramFiles\Git\bin\bash.exe" tools/selftest.sh
```

If `py` is a launcher rather than a directly executable interpreter in Bash, set `PYTHON` to the
full forward-slash path of `python.exe`. Do not mix WSL Bash with a Windows interpreter accidentally.

## What is deliberately not proven here

A production reverse proxy/TLS/read-auth boundary, a real deployment provider or canary, alert
delivery, backup restoration, browser-specific external-protocol prompts, the operator's worker
wrapper, project business behavior, or safety from an untrusted write-token holder. A closer must
name these as coverage gaps when they matter.

## CI

`.github/workflows/ci.yml` runs only `tools/check.sh --all-fast` on ordinary pushes and pull
requests. `.github/workflows/verify.yml` is manual-dispatch only and runs the selftest ladder —
verification stays a decision someone makes for a boundary, never a schedule.
