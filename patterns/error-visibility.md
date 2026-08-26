# Error Visibility — wire it before the first feature

The failure mode this pattern kills: **a service that can only be debugged from a shell on its
host**. A server 500, a background-job death, or a worker wrapper that exits before claiming
exists only in a log file nobody reads — so "is anything broken?" has to be asked once per
service, by someone who already suspects the answer, and therefore never gets asked. Measured on
the system this scaffold was extracted from: a background worker died at 00:48 and the only
signal was a person noticing at 08:25 that its queue had stopped.

The Hub ships the receiving half: the operational error stream (`hub_core/errorlog`), with
write-time redaction, per-fingerprint throttling, a read-time severity bar shared by the board
and the API, truthful ack/reopen, bounded clears, and per-channel **coverage** — the errors card
names every channel that CAN report and whether it has, because an empty card whose channels are
dark must never read as good news. This pattern is the SENDING half, which only you can wire.
Like every pattern here, nothing below is active merely by existing in the repository.

## What forwards, and what deliberately does not

Forward only what belongs on a queue a human is expected to drain:

- **server exceptions** (a 500 on a request path);
- **background-job deaths** (the scheduler tick that raised, the worker loop that exited);
- **worker-side operational failures** (a launcher that will not start, tooling that cannot
  write, a client refused upstream — `agent-error`).

Do NOT forward uncaught browser errors from your services' pages. A shared queue that fills
with other people's stale-tab noise is a queue everyone learns to ignore — the Hub board's own
browser has its narrow CSRF-gated `client-error` channel, and even those rows are held below
the bar by default.

## 1. The host app the Hub is mounted in — one LOGGING handler

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

Every 5xx on the mounted app now lands on the board with a stack trace, redacted, throttled.
`django.security.*` noise (a scanner's bad Host header) is classified as a foreign-client
warning and never crowds the queue.

## 2. A SATELLITE service — a bounded HTTP forwarder

A separate service (another app, a scheduled job, anything with credentials for a scoped
`error:report` Hub token) forwards through `POST /hub/api/app-error`, attributed to the APP by
slug so the board can say *which one*:

```python
import json, logging, os, traceback, urllib.request

class HubForwarder(logging.Handler):
    """ERROR+ -> the Hub's operational stream. Fail-soft by construction: forwarding must
    never become the reason a request fails, and a hub outage must never take the app down."""

    def emit(self, record):
        try:
            body = {
                "app": os.environ["APP_SLUG"],
                "kind": "job" if getattr(record, "background", False) else "server",
                "message": record.getMessage()[:800],
                "severity": "critical" if record.levelno >= logging.CRITICAL else "error",
                "details": "".join(traceback.format_exception(*record.exc_info))[:2000]
                           if record.exc_info else "",
                "component": record.name,
            }
            req = urllib.request.Request(
                os.environ["HUB_API_BASE"].rstrip("/") + "/api/app-error",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "X-Agent-Token": os.environ["HUB_AGENT_TOKEN"]})
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            self.handleError(record)   # local log only; never raise into the app
```

Issue that service its own scoped credential — never the root token:

```bash
python -m hub_core.client --url $HUB \
  # via a credential:manage holder:
  # POST /hub/api/agent-credential {"action":"issue","subject":"svc-billing",
  #                                 "scopes":["error:report"],"ttl_s":31536000}
```

A CLI process can forward without any handler wiring:

```bash
python -m hub_core.client app-error --app billing --kind job \
  --message "nightly export crashed" --details "$(tail -c 1800 job.log)"
python -m hub_core.client agent-error --agent worker-3 --source launcher \
  --message "wrapper exited 1 before claiming"
```

## 3. Drain the queue, honestly

An unclaimed row on the bar is unassigned work, not a status light. Claim it when you pick it
up, clear it only bounded:

```bash
python -m hub_core.client ack-error <fingerprint> --note "fixed in the export job"
python -m hub_core.client ack-error <fingerprint> --reopen     # it came back
# clears are bounded by AGE or ACK — never "everything":
# POST /hub/api/clear-errors {"only_acked": true}
```

## The properties to preserve if you adapt this

- **Fail-soft sending.** The forwarder swallows its own failures; a dead Hub must cost a
  service nothing but visibility — and the coverage block on the errors card is what tells the
  operator that channel went dark, so the silence is still seen.
- **Attribute to the APP, not a person's machine.** `app.<slug>.<kind>` is what lets the board
  answer "which one".
- **Only queue-worthy classes.** Every row on the bar should be something a person can act on;
  the bar demotes what they cannot (foreign clients, warnings, recovered transport blips) —
  visibly deferred, never dropped.
- **Secrets never ride.** The stream redacts at the door, and the Hub's write seam refuses
  secret-shaped payloads outright — but truncate details client-side anyway; a stack trace
  does not need the whole environment block.
