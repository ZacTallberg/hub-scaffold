# app-kit — a brand-free Django app kit that pairs with the Hub

The Hub (`hub_core/` + `adapters/django/`) is where a project's work and its failures are
recorded. The apps a project ships are separate services. `app-kit/` is the set of drop-in Django
pieces that make such an app a good citizen of the Hub from its first commit: its failures reach
the Hub's operational error stream, its readiness is a contract a deploy gate can read, and its
long-lived pages keep working after a sign-in rotates the CSRF token.

Nothing here carries a brand. Every visual default is a neutral design token on `:root` with a
dark-mode override; point the templates at your own base to inherit your shell. Nothing here is
active until you install it in an app — like every pattern in this repository, a kit that is not
wired is documentation, not a running control.

| Kit | What it gives an app | Pattern it implements |
|---|---|---|
| `kits/error-visibility/` (`app_errors`) | Automatic producers for every failure class (request exceptions, every ERROR log record classified, dying threads and processes, agentic-chat faults, the browser's JS/promise/request/stream/socket/worker/CSP failures), one local table with a read surface (`/errors/`, `errors/json/`, `manage.py errors`), a fail-soft recorder, and a forwarder to `POST /hub/api/app-error` that proves its own chain (arming row, delivery counters, `manage.py error_selftest`). | `patterns/error-visibility.md` |
| `kits/health/` (`health.py`) | Liveness and readiness views whose payload is the shape a deploy gate reads: `checks` as a list of `{name, status, detail}`, `"ok"` the only passing status, readiness that touches a real table. | `patterns/deploy-contract.md` → "Gate hygiene" |
| `kits/csrf/` (`app_csrf`) | The CSRF cookie NAME rendered from settings and a reader that takes the VALUE at send time, so a tab open across a sign-in does not 403 every write. | `kits/csrf/README.md` |

Other kits (an app shell, a sign-in gate, an assistant canon) may sit beside these under `kits/`;
each carries its own README.

## Install, in the order that matters

1. **Error visibility first — before the first feature.** A new app can fail invisibly: a JS
   exception, a failed request, a dead stream and a background-job death land nowhere, and a
   server 500 exists only in a service log on a host you may not have a shell on.
   See `kits/error-visibility/README.md`.
2. **Health**, so the deploy gate reads readiness, not liveness.
3. **CSRF at send time**, if any page issues writes and stays open.

## Proof

Follow the repository's proof policy (`docs/TESTING.md`): exercise the real operation — boot the
app, make a real failure, read the row on `/errors/` and on the Hub's errors card — and record what
happened. `manage.py error_selftest --forward` is that operation for the error kit: it fires every
producer for real and reads the Hub's answer. Do not add permanent tests around these kits.
