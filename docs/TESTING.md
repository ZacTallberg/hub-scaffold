# Testing and verification

The repository's test strategy keeps the default gate compact while concentrating assertions on
irreversible boundaries: event integrity, schema/write refusals, completion, launch authorization,
and a real mounted Django example.

## The required scaffold gate

```bash
bash tools/selftest.sh
```

The self-test runs five independent steps and prints a result for each:

1. agnosticism scrub (no origin-specific residue);
2. framework-free `hub_core` unit tests;
3. documentation checks (local links and mirrored schema drift);
4. generated bootstrap/template byte-integrity check;
5. Django example boot, seed, audit, API refusal ladder, strict completion, and launch-grant
   mint/consume/replay boundary.

Every step runs even if an earlier one fails, and the script exits nonzero if any step failed.

## Prerequisites

- Git and Bash;
- Python 3.10+ for `hub_core` and Django 5.2;
- Python 3.12+ if the environment resolves Django 6.0;
- dependencies from `requirements.txt` for the Django example.

The dependency range is intentionally `Django>=5.2,<6.1`: 5.2 is the supported LTS floor, 6.0
raises its Python floor to 3.12, and an untested future feature series must be admitted explicitly.
Check Django's
[official supported-version table](https://www.djangoproject.com/download/) and pin a tested,
current patch release in an adopting application.

## Platform recipes

Linux/macOS or a shell where `python` is the intended interpreter:

```bash
python -m pip install -r requirements.txt
bash tools/selftest.sh
```

If the interpreter has a different name:

```bash
PYTHON=python3 bash tools/selftest.sh
```

Windows PowerShell with Git for Windows:

```powershell
py -m pip install -r requirements.txt
$env:PYTHON = ((py -c "import sys; print(sys.executable)") -replace '\\', '/')
& "$env:ProgramFiles\Git\bin\bash.exe" tools/selftest.sh
```

If `py` is a launcher rather than a directly executable interpreter in Bash, set `PYTHON` to the
full forward-slash path of `python.exe` instead. Avoid accidentally invoking WSL Bash with a Windows
Python installation; WSL and Windows use separate filesystems/interpreters unless deliberately
wired together.

## Focused commands

```bash
python -m unittest discover -s hub_core -t . -v
python tools/docs_check.py
python tools/build_bootstrap.py --check
bash tools/scrub_check.sh
```

For the example only:

```bash
cd example
DEBUG=1 HUB_WRITE_TOKEN=selftest-token python manage.py migrate
DEBUG=1 HUB_WRITE_TOKEN=selftest-token python manage.py seedhub
DEBUG=1 HUB_WRITE_TOKEN=selftest-token python manage.py hubaudit
DEBUG=1 HUB_WRITE_TOKEN=selftest-token python selftest.py
```

The example intentionally uses `HUB_DONE_STRICTNESS="strict"` so it exercises the strongest shipped
completion ladder even though the adapter default is `tracked`.

## What is covered

- canonical event serialization, optimistic concurrency, idempotency, index reconciliation,
  torn-write recovery, append-only triggers, and hash-chain tamper detection;
- schema validation through seed/write/audit paths;
- settings and route audit integration through the example;
- fail-closed write authentication and malformed-request handling;
- queue discovery before claim, durable `in_progress` transition, live-lease exclusion, same-owner
  idempotent renewal, competing-owner refusal, expiry/reclaim fencing, completion cleanup, and refusal
  to claim missing or terminal tasks;
- direct-done refusal, live claim requirement, non-empty evidence, strict evidence dereference,
  required verification command, command failure/success, and critical-audit completion refusal;
- launch grant signatures, bounds, expiry, replay resistance, cross-process secret initialization,
  issuer validation, redirect refusal, browser UI contract, and Windows handler safety;
- documentation links, PROJECT/example schema parity, and generated bootstrap parity.

## What is deliberately not proven by this gate

- a production reverse proxy, TLS, read authentication, or network policy;
- a real deployment provider, front-door build canary, alert delivery, or backup restoration;
- browser-specific external-protocol prompts on every browser version;
- the operator-supplied worker wrapper or any vendor agent CLI;
- project-specific schemas, verification commands, migrations, or business behavior;
- security against an untrusted write-token holder.

Adopters should add tests only where they defend a concrete project boundary or previously observed
failure mode. Keep expensive or broad suites off the default path if they add little signal, but do
not remove refusal tests around authentication, command execution, evidence, event integrity, or
process launch: those are the boundaries where a false green has high consequence.

## CI

`.github/workflows/ci.yml` installs dependencies and runs the scrub plus full self-test on Python
3.10 (which resolves Django 5.2) and Python 3.12 (which resolves Django 6.0). This small matrix
directly proves the documented compatibility boundary. Make both jobs required status checks. A
workflow file in the repository is not itself enforcement until the repository host prevents a red
or missing check from merging.
