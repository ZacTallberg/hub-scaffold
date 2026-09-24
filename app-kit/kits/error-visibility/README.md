# error-visibility (`app_errors`) — every failure an app has, on the Hub

Implements `patterns/error-visibility.md` for a Django app. Copy `app_errors/` into the project
(it is a normal Django app; its label is `app_errors`).

## Wire it

```python
# settings.py
INSTALLED_APPS += ["app_errors"]

APP_SLUG = "budget-app"                                  # attribution on the Hub
HUB_API_BASE = os.environ.get("HUB_API_BASE", "")        # e.g. https://hub.example.com/hub
HUB_AGENT_TOKEN = os.environ.get("HUB_AGENT_TOKEN", "")  # scoped credential with error:report
# optional:
# HUB_CA_BUNDLE = "/etc/ssl/private-ca.pem"   # TLS verification stays ON; name a private CA
# APP_ERRORS_HUB_BRIDGE_PATHS = ("/banner/feed.json",)   # routes that proxy the HUB itself
# APP_ERRORS_BASE_TEMPLATE = "base.html"      # render /errors/ inside your own shell
```

```python
# urls.py
path("errors/", include("app_errors.urls")),
```

```html
<!-- in <head> of every page (the kit's own base.html already does this) -->
<script src="{% static 'app_errors/report_errors.js' %}"
        data-report-url="{% url 'errors_report' %}" defer></script>
```

Then `python manage.py migrate app_errors`. `AppConfig.ready()` arms every producer; there is
nothing to call. With `HUB_API_BASE`, `HUB_AGENT_TOKEN` or `APP_SLUG` unset the kit is local-only
(rows on `/errors/`, a red "forwarding NOT armed" chip) — never an error.

Issue the app its own scoped credential, never the root token:
`POST /hub/api/agent-credential {"action":"issue","subject":"budget-app","scopes":["error:report"],"ttl_s":31536000}`
from a `credential:manage` holder. `ttl_s` is required (60..31536000 seconds) and the credential
EXPIRES: issue its replacement, swap `HUB_AGENT_TOKEN`, then revoke the old one before the
`expires_at` in the issue answer. A lapsed or revoked token is refused with HTTP 403, and that
refusal shows up in `forwarding_status()` (`failed` climbing, `last_ok: false`,
`last_detail: "HTTP 403"`), on the `/errors/` forwarding chip, and as a `hub.auth` refusal row on
the Hub's error stream — never as a silent stop.

If several apps share one database, rename `APP_ERROR_TABLE` in `models.py` BEFORE the first
migrate.

## What it produces

| kind | producer |
|---|---|
| `server` | `got_request_exception` — a view raised |
| `django` / `data` / `other` | a root ERROR handler (also attached to every `django.*` logger that stops propagating); data-fault exception types are named literally |
| `background` | `threading.excepthook` and `sys.excepthook` |
| `agent` | `record_agent()` / `await record_agent_async()` and `agent_faults(stream)` |
| `js` `promise` `http` `stream` | `report_errors.js`: `error`, `unhandledrejection`, XHR, `fetch`, `WebSocket`, `EventSource`, workers, `console.error`, CSP violations, htmx events |
| `forwarder` | one `info` arming row per serving process (`error` if a producer failed to arm) |

Readiness 503s, handled upstream 502/504 answers and peer disconnects are sent as warnings; a
handled gateway answer on a declared hub-bridge route stays local. For code that catches its own
exception: `capture.record_background("nightly-export", exc)`. For an agent loop:

```python
async for event in capture.agent_faults(loop.run(turn), thread=thread_id, turn=n):
    yield event          # the stream is untouched; faults are recorded on the way through
```

## Read it

- `/errors/` — the heading states a finding, counts carry their denominator, the stack is in the
  row, and the forwarder's armed/delivered/failed state and the recorder's own fault count are
  shown so an empty table cannot be mistaken for a healthy one.
- `/errors/json/` — the same, for an agent with HTTP but no shell.
- `python manage.py errors [--since 2h] [--kind server] [--id N --trace] [--json] [--resolve N]`.

Put `/errors/` behind the app's own sign-in; it shows stack traces.

## Prove it

```bash
python manage.py error_selftest            # every producer fires for real; a row per channel
python manage.py error_selftest --forward  # ...and one info row to the Hub, answer read back
```

The selftest refuses to fabricate anything unless it has proven that forwarding suppression is
honoured, counts any sender that escaped, and resolves (never deletes) its probe rows. On the Hub,
the errors card's **forwarders** row shows the app as armed once a serving process has started.
