# health — two probes that are allowed to disagree

- `/health/live/` — is the process serving? Touches nothing else.
- `/health/ready/` — can it do its job? Reads a table `migrate` creates and refuses (`503`) while any
  migration is unapplied.

`SELECT 1` is not readiness: it succeeds against a database with no tables at all, so a deploy that
forgot to migrate reports ready and serves errors. A readiness probe byte-identical to liveness —
`200` with the database gone — is exactly the defect a deploy gate exists to catch.

Mount with `path("health/", include("app_health.urls"))`. The gate kit exempts both paths (a deploy
has no session); neither returns data.
