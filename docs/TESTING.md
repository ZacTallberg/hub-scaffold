# Testing and verification

Testing is a tool for resolving risk, not a ritual attached to every edit. Minor copy, formatting,
or obviously local changes do not automatically need executable tests, the integration battery, or
a second agent. A check belongs on the critical path only when it cheaply protects a concrete
failure mode.

## Four levels

| Level | Use when | Mechanism |
|---|---|---|
| Judgment | Tiny, low-risk, directly inspectable change | Read the diff; record truthful evidence. No command is mandatory. |
| Fast sanity | Ordinary pending work | `bash tools/check.sh`; it selects cheap checks from changed paths. |
| Focused proof | A specific behavior or regression changed | Run the smallest relevant test/probe, including the prior failure when possible. |
| Independent boundary verification | Release/deploy, security/auth, migration/destructive work, public API/schema compatibility, concurrency/process launch, regression, broad batch, or occasional sample | Launch one fresh read-only `verification-closer`; run `bash tools/selftest.sh` only if the boundary justifies the full integration battery. |

Independent closers are disposable. They receive the raw target and claim, return one
`PASS`/`FAIL`/`INCONCLUSIVE` verdict with evidence and gaps, and exit. They do not fix their own
findings or wait for more work. See [the prompt](../campaigns/verification-closer.md) and reusable
[$verification-closer skill](../skills/verification-closer/SKILL.md).

## Fast ordinary check

```bash
bash tools/check.sh
```

The default invocation always runs the agnosticism scrub, then selects only relevant cheap checks:

- Python syntax for changed Python;
- documentation links and schema-mirror parity for docs/templates/schemas;
- generated bootstrap parity for `PROJECT/` template changes;
- shell syntax for changed shell scripts.

It deliberately leaves behavior-test selection to the implementer. `--all-fast` additionally runs
the framework-free unit suite and the complete cheap set; it is the ordinary push/PR CI command:

```bash
bash tools/check.sh --all-fast
```

## Isolated full verifier

```bash
python -m pip install -r requirements.txt
bash tools/selftest.sh
```

Use this deliberately, not per minor task. It runs five layers:

1. agnosticism scrub;
2. framework-free `hub_core` unit tests;
3. documentation links and mirrored schemas;
4. generated bootstrap/template byte parity;
5. a real Django seed/audit plus queue, completion, launch-grant, negative-path, and race ladder.

The Django layer creates a unique temporary ledger and SQLite database under a guarded
`.selftest-tmp.*` directory and removes them when its process ends. Repeated or concurrent runs do
not append to `example/PROJECT/.hub`, inherit old leases, or contend on a shared example database.
Every layer still runs after an earlier failure so a boundary verifier receives a complete report.

## Selection examples

- README typo: inspect it; optionally run the selected fast doc check. No fresh verifier.
- Local refactor with unchanged behavior: syntax plus the nearest unit test, if one exists.
- Bug fix: reproduce the bug, apply the fix, rerun the same probe. Add a regression only if it
  protects the failure class economically.
- Authentication refusal or task-lease race: focused negative/success paths and a fresh closer.
- Release candidate: fresh closer; full isolated battery when its coverage is relevant.
- Periodic confidence audit: sample a coherent batch, not every completed task.

## Prerequisites and compatibility

- Fast checks: Git, Bash, and Python 3.10+; no Django installation is required.
- Full verification: dependencies from `requirements.txt`.
- Django 5.2 runs on Python 3.10+; Django 6.0 requires Python 3.12+.

The dependency range is `Django>=5.2,<6.1`: an untested future feature series must be admitted
explicitly. Check Django's [supported-version table](https://www.djangoproject.com/download/) and
pin a current patch release in an adopting application.

Windows PowerShell with Git for Windows:

```powershell
$env:PYTHON = ((py -c "import sys; print(sys.executable)") -replace '\\', '/')
& "$env:ProgramFiles\Git\bin\bash.exe" tools/check.sh

# Only for an explicitly chosen full verification boundary:
py -m pip install -r requirements.txt
& "$env:ProgramFiles\Git\bin\bash.exe" tools/selftest.sh
```

If `py` is a launcher rather than a directly executable interpreter in Bash, set `PYTHON` to the
full forward-slash path of `python.exe`. Do not mix WSL Bash with a Windows interpreter accidentally.

## Full-battery coverage

The isolated battery covers event serialization/integrity/OCC/idempotency, schema contracts,
Django route/auth refusals, queue lease/transition/reclaim truth, completion evidence/command/audit
and race fencing, launch grant/replay/issuer bounds, Windows handler safety, documentation links,
schema mirrors, and generated templates.

It deliberately does not prove a production reverse proxy/TLS/read-auth boundary, real deployment
provider or canary, alert delivery, backup restoration, browser-specific external-protocol prompts,
the operator's worker wrapper, project business behavior, or safety from an untrusted write-token
holder. A closer must name these as coverage gaps when they matter.

## CI

`.github/workflows/ci.yml` runs only `tools/check.sh --all-fast` on ordinary pushes and pull requests.
`.github/workflows/verify.yml` is a manually dispatched, disposable full verifier across Python
3.10/Django 5.2 and Python 3.12/Django 6.0. Invoke it for a justified boundary; it is intentionally
not a required per-change ceremony.
