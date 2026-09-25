# health — two probes that are allowed to disagree

- `/health/live/` — is the process serving? Touches nothing else. It reports `commit`: the
  checkout HEAD this PROCESS loaded (read from `.git` once, at import), with the deploy's own
  stamp beside it (`stamp_commit`) and `stamp_mismatch: true` when they disagree. A deploy whose
  restart failed, or whose rollback restored the old commit, leaves a new stamp beside a process
  serving old code; a gate that reads the stamp calls that live. Point the gate at `commit`.
- `/health/ready/` — can it do its job? Reads a table `migrate` creates and refuses (`503`) while any
  migration is unapplied.

`SELECT 1` is not readiness: it succeeds against a database with no tables at all, so a deploy that
forgot to migrate reports ready and serves errors. A readiness probe byte-identical to liveness —
`200` with the database gone — is exactly the defect a deploy gate exists to catch.

Mount with `path("health/", include("app_health.urls"))`. The gate kit exempts both paths (a deploy
has no session); neither returns data.

## `health.py` — the same pair as two drop-in views

`health.py` provides `health_live` and `health_ready`. Wire both and point the deploy gate at
READY (liveness passes with the database down):

```python
from health import health_live, health_ready
urlpatterns += [path("health/live/", health_live), path("health/ready/", health_ready)]
```

The readiness payload is the contract in `patterns/deploy-contract.md` → "Gate hygiene":
`checks` is a list of `{name, status, detail}`, `"ok"` is the only passing status (a mode such as
"forwarder unarmed" is `ok` with the mode in `detail`), an empty `checks` fails, and the database
check touches a real table and refuses pending migrations. Append your own checks to `CHECKS`.
A 503 here during warm-up is recorded by the error kit as a warning, not a fault.
