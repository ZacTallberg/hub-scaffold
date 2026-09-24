# Error Visibility — wire it before the first feature

The failure mode this pattern kills: **a service that can only be debugged from a shell on its
host**. A server 500, a background-job death, a JavaScript exception, or a worker wrapper that
exits before claiming exists only in a log file nobody reads — so "is anything broken?" has to be
asked once per service, by someone who already suspects the answer, and therefore never gets
asked. Measured on the system this scaffold was extracted from: a background worker died at 00:48
and the only signal was a person noticing at 08:25 that its queue had stopped.

The Hub ships the receiving half: the operational error stream (`hub_core/errorlog`), with
write-time redaction, per-fingerprint throttling, a read-time severity bar shared by the board
and the API, truthful ack/reopen, bounded clears, per-channel **coverage**, and a **forwarders**
row that says which satellites have proven their chain. This pattern is the SENDING half, which
only you can wire. A ready-made Django implementation of everything below lives in
`app-kit/kits/error-visibility/`. Like every pattern here, nothing is active merely by existing in
the repository.

## What forwards: every class, with noise discipline at the sender

Forward **every** class of failure a service can have — server exceptions, background-job deaths,
framework and data faults, faults inside an agentic chat loop, and the browser's own failures
(uncaught JS, unhandled rejections, failed requests, dead live streams). An earlier revision of
this pattern said "do not forward browser errors"; that left every browser-side failure reaching
nobody. Volume is controlled by making each row **actionable**, never by dropping a class — a
dropped report destroys the one copy nobody else holds. The read-time bar then keeps warnings,
foreign clients and recovered transport blips off the queue while still counting them.

Kinds a sender names (`kind` on `POST /hub/api/app-error`):

| kind | produced by | default severity sent |
|---|---|---|
| `server` | the framework's request-exception signal (a view raised) | error |
| `background` | `threading.excepthook` / `sys.excepthook` (a thread or process died) | error |
| `django` / `data` / `other` | a root logging handler on every ERROR+ record, classified | error |
| `agent` | a wrapper around an agent loop's event stream (see §5) | error |
| `js` / `promise` / `http` / `stream` | the browser reporter (see §4) | error |
| `forwarder` | the startup arming row (see §3) | **info** |

## 1. Producers are AUTOMATIC — hook the platform, not each author's try/except

A kind that nothing produces is a silent channel. Every producer is armed once, at startup, by
the service itself:

- **the request-exception signal** for a view that raised;
- **one root logging handler** at ERROR that classifies each record: a named set of exception
  types that mean *the data was wrong* (`IntegrityError`, `JSONDecodeError`, `UnicodeDecodeError`,
  …) is `data`; any `django.*` logger is `django`; everything else is `other`. Classification
  fails toward a mislabelled row, never a missing one;
- **the same handler attached to every framework logger that stops propagating** — a project that
  gives `django.request` its own handlers with `propagate=False` otherwise leaves a view's
  *returned* 5xx dark, the one case the request signal cannot see;
- **`threading.excepthook` and `sys.excepthook`**, chained to whatever was there before;
- **guards**: a per-thread re-entrancy flag so a failing write that logs cannot loop; one row per
  log record however many loggers deliver it; a record the caller already stored is marked
  (`extra={"app_errors_recorded": True}`) so one failure is one problem.

Severity sent to the hub is uniform (`error`) except for designed degradation (§2): only the hub
sees every service at once, so escalation is the hub's call. A local read surface may still rank
by kind (server-side kinds critical; a data fault escalates when it recurs).

## 2. Designed degradation is not an app fault

These are recorded, and demoted or kept local — keyed on status and path, never on message text:

- a **readiness probe's 503** on `/health/ready` or `/health/live` is the probe WORKING during
  warm-up → `warning`;
- a view that **answers** 502/504 (no exception) is reporting its upstream → `warning`. A 503 from
  any other view is the service saying *it* is unavailable and still pages;
- a **peer hanging up** (connection reset/aborted, broken pipe on the event loop) → `warning`;
- a handled 502/503/504 on a route that **proxies the hub itself** (a hub-hosted feed or banner)
  is recorded locally and **not forwarded**: the hub already knows it is down, twenty satellites
  each filing its outage as their own fault is noise, and a problem's severity may freeze at its
  first row. The routes are declared by the service (`APP_ERRORS_HUB_BRIDGE_PATHS`), never guessed.

The hub's own mounted app applies the first two through `errorlog.HubErrorHandler`.

## 3. The forwarder proves its own chain

"No rows" from a satellite cannot tell *nothing failed* from *the forwarder is unconfigured*. So:

- **Arming row.** Each serving process (not every management command) sends exactly one
  `severity: info`, `kind: forwarder`, `code: app_forwarder_armed` row at startup naming which
  producers armed; it is sent at `error` when one failed to. The hub records `info` rows, shows
  them under the errors card's **forwarders** row (`coverage.forwarders` in the API), and never
  puts them on the bar.
- **Delivery status.** The sender counts `sent` / `failed` and keeps `last_ok` / `last_detail`
  (an HTTP status or an exception class name, nothing else) — safe to publish on a readiness
  check; the token never appears.
- **Installer failures are loud.** A producer that failed to install says so on stderr and in the
  arming row.
- **A test run never forwards** on the automatic path (a fixture's fabricated failure must not
  reach the live board); a deliberate opt-in setting exists for a suite that wants it.
- **A selftest that cannot poison the board.** `error_selftest` fires every producer for real (a
  logging call, a dying thread, the request signal, the agent entry point), holds forwarding down
  for the run, **proves the suppression switch is read before fabricating anything**, counts any
  sender that escaped, resolves (never deletes) its probe rows, and with `--forward` sends one
  `info` row and reads the hub's answer.
- **One configuration resolution** is shared by the forwarder and the selftest, so the probe can
  never report a gap the forwarder does not have (or miss one it does).

## 4. The browser reporter: every channel, only actionable rows

Hook the platform objects — `error`, `unhandledrejection`, `XMLHttpRequest`, `fetch`,
`WebSocket`, `EventSource`, `Worker`/service worker, `console.error`, CSP violations — because a
channel that needs each page to remember is dark on the pages that forgot. Then keep rows
actionable:

- a rejection whose reason is not an `Error` is **described** (`HTTP 502 /x`, `Object {…}`), never
  sent as `[object Object]`; a rejection with no reason is reported once per page;
- a **background poll** or a background **stream** must fail **twice in a row** before it is a row;
  one blip that recovered is not a defect;
- page **teardown** (`pagehide`/`beforeunload`), a browser with **no network**, and an abort the
  page itself caused stay silent;
- an `img src=""` clear is not a failed load; an element marked `data-reports-own-errors` reports
  through its own path;
- a library's event-name narration on `console.error` is dropped;
- per-page and per-signature caps; reporting never throws.

The hub board applies the same rules to itself through the CSRF-gated `client-error` channel.

## 5. Agentic-chat faults are a kind

A failed turn inside an agent loop is shown to the one person watching and reaches nobody else
unless it is recorded. Record it as `kind: agent` with **thread, turn and tool — never the
prompt**. Wrap the loop's event stream so every loop-level error event, tool exception, tool
timeout and unavailable source is recorded while the stream passes through untouched; a
*designed* refusal (a denied write, a scope guard, a missing input) is not a fault. From async
code, await the recorder on a worker thread — a synchronous ORM write from a running event loop
is refused, and a fail-soft recorder then silently writes nothing.

## 6. Tracebacks are kept for their END

The exception type, its message and the raising frame are at the **bottom** of a trace. A
`[:N]` cut keeps the banner and throws the cause away. Keep **head and tail** and state the gap
(`… N characters elided …`); the hub does the same (`errorlog.fit_details`, 32 KB) and lifts the
final `ExceptionType: message` line onto the row as `cause`.

## 7. The wire contract

```
POST <hub>/hub/api/app-error      X-Agent-Token: <scoped token with error:report>
{"app", "kind", "severity", "message", "details", "code", "path", "operation", "host"}
```

and nothing else. **No `agent` field**: the scoped token already names the sender, and a body
field that disagrees with the credential's subject is exactly what a hub may refuse. Keep one
canonical sender; if services vendor copies, check every copy against this contract (a structural
check of the payload keys and header name) rather than trusting that they were copied faithfully.
TLS verification stays on — name a private CA bundle (`HUB_CA_BUNDLE`), never disable checking.

The same payload is reachable from every surface: `python -m hub_core.client app-error --app …
--kind … --message …`, the MCP tool `report_app_error`, and the HTTP route above.

## 8. The host app the Hub is mounted in — one LOGGING handler

```python
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "hub_errors": {
            "()": "hub_core.errorlog.HubErrorHandler",
            "hub_dir": str(BASE_DIR / "PROJECT" / ".hub"),
            "level": "ERROR",
        },
    },
    "loggers": {
        "django.request": {"handlers": ["hub_errors"], "level": "ERROR", "propagate": True},
    },
}
```

Every 5xx on the mounted app lands on the board with a stack trace, redacted, throttled;
readiness 503s and handled upstream 502/504s land as warnings; `django.security.*` noise is a
foreign-client warning.

## 9. A satellite without the kit — a minimal forwarder

```python
import json, logging, os, traceback, urllib.request
from hub_core.errorlog import fit_details   # or copy it: keep head AND tail

class HubForwarder(logging.Handler):
    """ERROR+ -> the Hub's operational stream. Fail-soft: forwarding never fails a request."""

    def emit(self, record):
        try:
            details = ("".join(traceback.format_exception(*record.exc_info))
                       if record.exc_info else "")
            body = {"app": os.environ["APP_SLUG"],
                    "kind": "background" if getattr(record, "background", False) else "server",
                    "severity": "error", "message": record.getMessage()[:800],
                    "details": fit_details(details), "operation": record.name}
            req = urllib.request.Request(
                os.environ["HUB_API_BASE"].rstrip("/") + "/api/app-error",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "X-Agent-Token": os.environ["HUB_AGENT_TOKEN"]})
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            self.handleError(record)   # local log only; never raise into the app
```

Issue that service its own scoped credential — never the root token
(`POST /hub/api/agent-credential {"action":"issue","subject":"svc-billing","scopes":["error:report"],"ttl_s":31536000}`
from a `credential:manage` holder). `ttl_s` is required (60..31536000 seconds) and the credential
expires: rotate it before the `expires_at` the issue answer names — issue the replacement, swap
the service's token, revoke the old one. A lapsed token is refused with HTTP 403; the app kit's
`forwarding_status()` (and its forwarding chip) shows it as a climbing `failed` count with
`last_detail: "HTTP 403"`, and the Hub records the refusal as a `hub.auth` row, so an expired
forwarder is visible instead of silently unarmed. A CLI process forwards without any wiring:

```bash
python -m hub_core.client app-error --app billing --kind background \
  --message "nightly export crashed" --details "$(tail -c 30000 job.log)"
python -m hub_core.client agent-error --agent worker-3 --source launcher \
  --message "wrapper exited 1 before claiming"
```

## 10. The recorder inside the service (when it keeps a local table too)

A service that stores its failures locally before forwarding must keep that recorder from
becoming a second failure:

- **fail-soft, and its own death is audible** — `record()` never raises; when it cannot write it
  says so on stderr and forwards a `critical` `app_recorder_failed` row on a path that cannot
  re-enter the recorder, bounded by interval and a per-process ceiling;
- **a failed insert cannot poison the caller's transaction** — the write runs in a savepoint;
- **a record from inside a running event loop hops to a worker thread** that closes its own
  database connection; the burst cap is spent once, before the hop;
- **identical failures are one row with a count**, volatile fragments (ids, addresses, numbers)
  stripped from the fingerprint, and a recurrence reopens a resolved row.

## 11. Drain the queue, honestly

An unclaimed row on the bar is unassigned work, not a status light. Claim it when you pick it up,
clear it only bounded:

```bash
python -m hub_core.client ack-error <fingerprint> --note "fixed in the export job"
python -m hub_core.client ack-error <fingerprint> --reopen     # it came back
# POST /hub/api/clear-errors {"only_acked": true}   -- bounded by AGE or ACK, never "everything"
```

## The properties to preserve if you adapt this

- **Fail-soft sending, loud failure to send.** A dead hub costs a service nothing but visibility,
  and the forwarder's own counters plus the hub's coverage and forwarders rows make that silence
  visible.
- **Attribute to the APP, not a person's machine.** `app.<slug>.<kind>` answers "which one".
- **Every class forwards; every row is actionable.** Noise is removed at the sender by rules that
  make a row mean something, and at read time by the bar — visibly deferred, never dropped.
- **Secrets never ride.** The stream redacts at the door and the write seam refuses
  secret-shaped payloads; never send a prompt, a request body or the environment block.
