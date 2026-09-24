# app-kit — a brand-free Django app kit that pairs with the hub

The hub (`hub_core/`, `adapters/django/`) is where a team coordinates work. **app-kit** is for the
apps that work produces: small internal Django apps that should ship finished, fail closed, and be
debuggable from outside the host. It is a set of kits you vendor into an app, a bar the app is
measured against, and the tools that keep both honest. Nothing in it names a company, a host, a
person or a colour scheme — the design tokens are neutral and meant to be replaced.

It pairs with the hub in two places: an app's server errors are forwarded to the hub's operational
error stream (`POST /hub/api/app-error`, the hub's `patterns/error-visibility.md`), and app work is
claimed, proven and finished on the hub like any other task — the audit and the probes below
produce the receipts.

## What is here

| path | what it is |
|---|---|
| `kits/settings` | one `configure()` call: fail-closed settings, a fixed test posture, and `envcheck` — a production environment validator that names **every** problem in one throw |
| `kits/gate` | deny-by-default identity gate: owners by name, a per-app roster with a role ladder, bootstrap allowlist, append-only gate log, `manage.py roster`, boot checks that refuse a half-configured gate |
| `kits/shell` | the app chrome: theme before first paint, one keyboard-scrollable region, and capabilities (toasts, palette, live status, drawer, confirm, transitions, intent prefetch) that are inert until the app declares them |
| `kits/health` | liveness, and a readiness probe that fails on an unmigrated database (`app_health`, mountable); `health.py` is the same pair as two drop-in views whose payload is the deploy gate's `checks` contract |
| `kits/error-visibility` (`app_errors`) | automatic producers for every failure class (request exceptions, every ERROR log record classified, dying threads and processes, agentic-chat faults, the browser's own failures), a local `/errors/` table, forwarding to the hub's operational stream with a startup arming row, and `manage.py error_selftest --forward` |
| `kits/csrf` (`app_csrf`) | the CSRF cookie NAME rendered from settings and a reader that takes the VALUE at send time, so a tab open across a sign-in does not 403 every write |
| `kits/service-runner` | run, supervise and verify declared services from a strict manifest; a requested stop is not a crash; `--verify` asserts the gate posture |
| `kits/assistant` | the Assistant Canon (64 families) as data, the coverage count, routing lanes, the turn plan, the correction belt, the three write models and a real-model probe |
| `BAR.md` | the bar, in prose — what "done" means, rule by rule |
| `tools/app_audit.py` | measures an app against the bar (`--count` prints the bar's size; `--rules` audits the bar itself) |
| `tools/kits.py` | records what the kit ships (fails closed on a kit that is not self-sufficient), vendors a kit into an app with provenance, reports drift |
| `example/` | a small budget app assembled from the kits — the reference, not a template to copy |

## Start an app

1. **Vendor the kits** (from an up-to-date checkout — `add` refuses one that is behind):

   ```
   python app-kit/tools/kits.py add settings path/to/app
   python app-kit/tools/kits.py add gate path/to/app
   python app-kit/tools/kits.py add shell path/to/app
   python app-kit/tools/kits.py add health path/to/app
   ```

   Each `add` prints the kit's `REQUIRES.md` — the seams your app must supply — and records the
   kit's sha in the app's `.app-kit.json`.
2. **Configure** with `appkit_settings.configure(globals(), BASE_DIR, app_module=...,
   root_urlconf=..., gate_table_prefix="your_app")`, and mount `app_gate.urls`, `app_health.urls`
   and `*appkit_settings.static_urlpatterns()`.
3. **Wire error forwarding before the first feature** — `kits/error-visibility` (`app_errors`)
   is that wiring; see its README and the hub's `patterns/error-visibility.md`. A new app can fail
   invisibly: a JS exception, a failed request, a dead stream and a background-job death land
   nowhere, and a server 500 exists only in a service log on a host you may not have a shell on.
   Add `kits/csrf` if any page issues writes and stays open.
4. **Build to the bar**, then measure: `python app-kit/tools/app_audit.py path/to/app -v`.
5. **Prove it by the real operation**: sign in, use the page, read what came back. Before deploying,
   `python kits/settings/envcheck.py .env` names every missing key at once; after deploying,
   `service_runner.py --verify <name>` asserts health and the gate posture.

Later: `python app-kit/tools/kits.py status path/to/app` says, per vendored kit, whether it is
current, stale (the kit changed since), edited locally (an update would overwrite that), or
retired (and what replaced it).

## Run the example

```
cd app-kit/example
set DJANGO_DEBUG=true & set APP_GATE_SUPERADMINS=alice          (PowerShell: $env:NAME="value")
python manage.py migrate
python manage.py budget_sample                                   # labelled sample lines
python manage.py shell -c "from django.contrib.auth.models import User; User.objects.create_user('alice', password='...')"
python ../kits/service-runner/service_runner.py --manifest deploy/services.json --console budget-app
```

Sign in as `alice` (an owner, so admitted by name). `manage.py roster --add bob --role member`
opens it to a second person; `bob` can read but not archive. The home page heading is computed from
the data, the table polls with a fingerprint (204 when nothing changed), rows open in the drawer,
Ctrl/Cmd-K searches, and "Archive N approved" refuses if the count moved since the page was drawn.

```
python ../tools/app_audit.py . -v
set DJANGO_SETTINGS_MODULE=config.settings & python -m assistant.audit    (with ../kits/assistant on PYTHONPATH)
```

## Keeping the kit honest

- **Provenance, not memory.** `kits.py record` writes `kits.json`: a sha per file and per kit.
  `add` copies it into the app, `status` compares. A kit is `active` or `retired` (with
  `replaced_by`) in its `KIT.json`; a retired kit cannot be vendored.
- **Self-sufficient kits.** `record` refuses a kit whose Python imports leave the kit (other than
  the standard library, Django, or a seam declared in its `REQUIRES.md`), whose templates reference
  a static file or template it does not ship, or whose templates carry a multi-line `{# #}` (it
  renders as page text). It also refuses docs that type the bar's size or describe a rule the audit
  does not define. Every problem is listed in one run.
- **Stale trees say so.** Every tool that reads a working tree prints
  `[checkout is N behind origin/main]` when it lags, and the tools that WRITE (`kits.py add`)
  refuse a stale checkout unless told `--anyway`. When staleness cannot be determined the tools
  print nothing rather than claim the tree is current. Fetch before you measure.
- **No standing test suite.** The proof of a change is the real operation, with a transient probe
  only at a critical boundary (see the repository's `docs/TESTING.md`). The kits carry none.
