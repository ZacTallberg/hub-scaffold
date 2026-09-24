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

Browser failures from your services' pages are the one class to forward with care. A shared
queue that fills with other people's stale-tab noise is a queue everyone learns to ignore, so if
a service forwards them (`kind: "browser"`), it must word a no-response failure the way §5 below
describes — the bar then holds those below the queue, counted and one query away, while a real
script fault still reaches it. The Hub board's own browser has its narrow CSRF-gated
`client-error` channel under the same bar.

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

## 4. Prove the sending half is SERVED, not merely installed

An installer that copies a reporter script into a service and says "placed" has proven a file
exists. It has not proven a browser can load it. On the system this pattern came from, the
reporter landed in a directory the framework's static finders only saw under one spelling of the
static-files setting; under another, every page linked a 404 and rendered identically — the
service looked armed and had no browser capture at all. Two halves, because either alone leaves
the defect reachable:

- **Place assets where the framework finds them wherever it is installed** — inside the package
  or app that ships them (for Django, the app's own `static/` and `templates/`, found by the app
  directories finder), never a project-root directory that is a source only under one setting.
- **Check the served URL, not the disk.** Fetch the asset's URL when something is listening;
  otherwise resolve it through the framework's own finders. Say which of the two answered. A check
  that cannot run reports `unchecked` and asks for a fetch — it never reads as a pass, which is
  exactly the failure mode that let a silent 404 live. The check must never break the install.

## 5. Word a no-response failure so the bar can recognise it

The read-time bar defers transport blips — a request that got no response at all, a fetch the
browser itself cancelled — because nobody can fix a laptop that went to sleep. It can only do that
when the message SAYS it was a blip. For a service's own browser reporter (`kind: "browser"`,
source `app.<slug>.browser`) the bar recognises:

- the bare engine wordings (`Failed to fetch`, `NetworkError ...`, `Load failed`) and every
  engine's cancellation wording (`AbortError: The user aborted a request.` / `The operation was
  aborted.` / `signal is aborted without reason` / `Fetch is aborted`);
- the same failure wrapped once in the reporter's own words: `request failed: GET <path> - Failed
  to fetch` and `live stream failed: TypeError: Failed to fetch`.

The wrapped rule is anchored at the END of the transport text on purpose. A background poll that
fails ONCE is a blip; one that fails several times in a row is an outage, and the reporter should
say so — `live stream failed: TypeError: Failed to fetch (2 consecutive background attempts, no
response)` — which does NOT match and therefore queues. Abort your own timeouts with a named reason
(`Hub did not answer <path> within N seconds`, as the board's `timedFetch` does) so a bare
`AbortError` can only mean the browser cancelled the request; the bar accepts that wording bare or
as `String(err)` writes it (`TimeoutError: Hub did not answer …`). Every rule is anchored at BOTH
ends: the message must be the transport wording and nothing more, so `The operation was aborted
because the store is corrupt` is a fault and queues. Report the wording verbatim — do not append
context to a blip you want deferred; put context in `context`, not `message`.

## 6. Show each service its own recent errors

A service that forwards is still read from the board — by someone who goes looking. Put the same
rows in the service's own chrome: `GET /hub/errors.json?app=<slug>` returns that service's slice
with the queue's counts computed over the same slice (`on_board`, `unclaimed`, `claimed`,
`oldest_unclaimed_s`, `deferred`). `python -m hub_core.client errors --app <slug>` and the MCP
`read_errors` tool read the same thing. The properties worth keeping in the consumer:

- **Fetch it same-origin from the page, with the viewer's own hub session** — never embed a
  token in the page. When the hub is behind sign-in, render a "sign in to see errors" state; when
  it is unreachable, say "error feed unavailable", never an empty list that reads as healthy.
- **Collapsed, carrying its count** ("Recent errors · 3 unclaimed"), and a capped list says it
  is capped ("showing 5 of 12").
- **One control**, not one per row: the drill-in is the board, where claim and resolve live.

A minimal consumer for a service whose pages share an origin with the hub (`HUB` is the hub's
mount path, `SLUG` the service's own):

```js
async function recentErrors(box, HUB, SLUG, cap = 5) {
  let res;
  try {
    res = await fetch(`${HUB}/errors.json?app=${encodeURIComponent(SLUG)}`,
                      { credentials: "same-origin", signal: AbortSignal.timeout(8000) });
  } catch (e) { box.textContent = "Error feed unavailable"; return; }
  if (res.status === 401 || res.status === 403 || res.redirected) {
    box.textContent = "Sign in to the hub to see this app's errors"; return;
  }
  if (!res.ok) { box.textContent = `Error feed unavailable (HTTP ${res.status})`; return; }
  const { data = [], metadata: m = {} } = await res.json();
  const open = data.filter(r => !r.acked);
  const summary = `Recent errors · ${m.unclaimed ?? open.length} unclaimed` +
                  (m.deferred ? ` · ${m.deferred} below the bar` : "");
  const shown = open.slice(0, cap);
  box.replaceChildren(Object.assign(document.createElement("details"), {
    innerHTML: `<summary></summary><ul></ul><p></p>` }));
  box.querySelector("summary").textContent = summary;
  for (const r of shown) {
    box.querySelector("ul").append(Object.assign(document.createElement("li"),
                                                 { textContent: r.message }));
  }
  box.querySelector("p").textContent = (open.length > cap ? `showing ${cap} of ${open.length} · ` : "")
                                       + (open.length ? "" : "nothing unclaimed · ") + "open the board to claim";
}
```

## 7. A dead channel is only caught by a SECOND, independent channel

The coverage block names each channel that CAN report and when it last did (`age_s`, shown as
"last 3h ago" on each chip) — the age travels as text, not only as a colour, because a channel that
reported a minute ago and one that reported three days ago are different evidence. But a channel
that dies SILENTLY looks exactly like a quiet one from inside itself; no amount of reading the
channel's own rows can tell the two apart. Only a second record that the first one SHOULD have
matched can. On the system this pattern came from, a CI-to-board webhook went dead and every app
still read as observed, until the verdict compared it against the deploy ledger: a deploy record
more than an hour newer than the newest CI event proves the webhook missed a pipeline, and drops
the app off "observed". Where you hold two independent records of the same activity (deploys and
CI events; a scheduler's run log and the job's forwarded failures; a heartbeat and a completion
count), derive a verdict from their DISAGREEMENT — `never` / `delivering` / `missed` / `quiet` —
rather than from either one's silence.

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
