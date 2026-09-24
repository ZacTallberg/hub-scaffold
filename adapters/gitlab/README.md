# GitLab adapter

`ensure_ci_hooks.py` registers the hub's CI webhook (`POST <hub>/api/ci-event`, see
`adapters/django/HUB-API.md`) on every project in scope. Create-only and report-by-default: it
creates missing project hooks and enables pipeline/job events on hooks that already point at the
hub, never deletes or edits a hook pointing anywhere else, and reports a project whose hooks it
cannot list as `unknown` (exit 3), never `ok`. Run it on a schedule so projects created later are
wired too. Project hooks are used rather than group hooks because group hooks can exist, pass a
manual test, and never fire on a tier that does not fan them out.

Any other CI system can post the generic event shape from a pipeline step instead:
`python -m hub_core.client ci-report --kind job --project budget-app --job test --status failed`.
