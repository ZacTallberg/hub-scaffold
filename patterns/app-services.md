# App services — what the hub gives the apps around it

A project rarely ships only its hub. It ships apps — a budget tool, an intake form, a report —
and each app wants the same few things: shared chrome that stays consistent, a way to show its
people what is being built for it, their preferences, and an assistant. Built per app, each of
those becomes N drifting copies. Built once at the hub, they are one thing every app links.

Four services, each with a narrow trust boundary. Engines are framework-free in `hub_core/`
(`components.py`, `app_feed.py`, `profiles.py`, `agent_broker.py`); the Django edge is
`adapters/django/hub/app_services.py`; the routes are in `adapters/django/HUB-API.md`. The
example site carries a runnable adopting app (`example/example_site/demo_app.py`, at
`/demo-app/`) that uses all four.

## 1. Hosted components — link, never copy

A component is a directory `hub_core/components/<name>/` holding its CSS/JS and a
`manifest.json` (title, what, link tags, mount markup, optional `props` schema). The hub serves
the files at `/hub/components/<name>/<file>` and lists them at `/hub/components/`.

- **The copy served is the master.** An app links two tags; a fix to the component reaches every
  app on its next page load (5-minute cache), with no app deploy. When apps share the hub's
  origin behind one reverse proxy, the link is same-origin with no build step.
- **Versions are measured.** The version is a hash of the served bytes, so it cannot disagree
  with them. Adoption is **observed**: a component's own properties fetch names itself
  (`?component=<name>`), and the index lists the apps seen loading it — never a declaration
  someone forgot to update.
- **Presentation only.** No board record and no identity is ever served here, which is why these
  reads can sit outside any read-auth boundary you add. Only files on disk under the component,
  with an allowed suffix, are served — there is no caller-controlled path.

Shipped component: `agent` (below). Add one by creating its directory; nothing else changes.

## 2. Per-app component properties — schema travels with the component

A component declares its settings in its manifest (`props`: fields of type `choice`, `multi`,
`text`, `list`, each closed or length-capped). Per app, an operator sets them:

    python -m hub_core.client component-props --app budget-app \
      --set agent.greeting="Ask about budgets" --set 'agent.entry=["float","header"]'

- The write needs `component:configure` — it changes what EVERY person on that app sees, so it
  is an operator credential, not something a browser can do.
- A save is a FULL set: a property left out returns to its default. Only non-default values are
  stored, so a later change to a default reaches every app that never chose otherwise.
- A value outside its field is **refused and listed**, never clamped to a neighbour.
- Components read `GET /hub/components/props/<slug>.json` on load and also honour
  `window.HubComponentProps` and a `hub:component-props` window event (`detail: {app, props}`),
  so an editor can preview a change without a reload.

## 3. One app's slice of the board

`GET /hub/app-feed.json?app=<slug>&name=<display name>` returns the app's **checklist** (open
tasks naming it), **announcements** (notes tagged `built-on-request` naming it — a capability
delivered without telling the person who asked is one they never learn they have), and an
empty **what's new** (a deploy record is not a change note; the app replaces that key with its
own user-language notes). Matching is textual and every row says which field matched; the
metadata carries shown and matched counts so a capped panel never reads as the whole list.

## 4. A person's preferences, and the brokered agent

Both follow one shape — **the hub authenticates the app, the app vouches for the person**:

    reader's browser  ->  the app's own gated endpoint (its bridge)
                      ->  /hub/api/...   with the APP's scoped credential and ?person=<name>

- `/hub/api/profile` holds a person's theme, motion, text size, UI scale, agent placement and
  mark (initials, or a small PNG/JPEG/WebP data URL; SVG refused). Closed sets; out-of-range
  values are dropped and reported. Mutable per-person state lives in `HUB_DIR/profiles.json`,
  not the ledger.
- `/hub/api/agent/*` forwards questions to one agent service with the **one** agent key the hub
  holds (`HUB_AGENT_URL`, `HUB_AGENT_KEY`, `HUB_AGENT_CONTEXT`, `HUB_AGENT_LABEL`,
  `HUB_AGENT_TIMEOUT_S`, `HUB_AGENT_TLS_VERIFY`). Apps never hold it, so there is one credential
  to rotate and audit.

Give each app's bridge a scoped credential with exactly what it forwards:

    POST /hub/api/agent-credential {"action":"issue","subject":"app:budget-app",
      "scopes":["agent:ask","agent:history","profile:read"],"ttl_s":2592000}

Link the agent component only from pages already behind the app's sign-in: it trusts the
bridge's naming of the person because the hub cannot see the reader.

### The agent service's upstream contract

Put this in front of whichever agent service you run (the hub speaks nothing else):

| Call | Body / query | Answer |
|---|---|---|
| `POST {HUB_AGENT_URL}/ask` | `query` (question with recent turns), `question` (as typed), `context`, `app`, optional `on_behalf_of`, `conversation_id` | `{answer, citations?: [{label\|title, href?, page?}], conversation_id?}` |
| `GET {HUB_AGENT_URL}/conversations` | `on_behalf_of`, optional `app`, `limit` | `{conversations: [{id, title, app, updated_at}], truncated?}` |
| `GET {HUB_AGENT_URL}/conversations/<id>` | `on_behalf_of` | `{conversation: {id, title, turns: [{role: you\|agent, text}]}}` |

Errors are non-2xx with `{code?, error?}`. Two codes are understood: `conversation_not_found`
(the question is answered as a new conversation) and `delegation_forbidden` (the question is
still answered, as the service, and the reply says history is off). Every other failure is
reported as itself; an empty `answer` is a fault, never a blank reply.

### The agent component

    <link rel="stylesheet" href="/hub/components/agent/agent.css">
    <script src="/hub/components/agent/agent.js" defer></script>
    <div data-hub-agent data-app="budget-app" data-title="Budget assistant"
         data-ask="/budget/agent/ask" data-history="/budget/agent/history"
         data-conversation="/budget/agent/conversation" data-profile="/budget/profile.json"
         data-props="/hub/components/props/budget-app.json"></div>

A launcher (floating button, right-edge tab, or a button in an element marked
`data-hub-agent-header`) chosen from the app's allowed entry points and the person's own
placement preference; a docked, resizable panel that pushes the page instead of covering it
(full screen on a phone); past conversations for this app or all apps; and distinct unconfigured,
failed (with retry) and empty states. Any element with `data-hub-agent-open`, or a
`hub:agent-open` window event (`detail: {question}`), opens it. Agent text is rendered as text,
never HTML.
