# The bar

"Done" for an app built with this kit is not "it works". It is measured:

```
python app-kit/tools/app_audit.py <app-dir> -v
python app-kit/tools/app_audit.py --count        # the bar's own size, today
```

Ship at 90 or above with **zero** MUST failures. Every gap the audit prints names the bar AND the
kit or pattern that closes it. Quote the counts the tool prints on the day you run it — never a
number from a document, including this one; the rule table lives in the tool, and `kits.py record`
refuses a doc that types the bar's size.

The rules below are listed in the order a person meets them. **MUST** marks a rule that blocks a
ship on its own.

## Honest numbers

- **title-is-a-finding** (MUST) — a heading states what was FOUND ("4 lines over budget"), not what
  the page is called ("Budget"). Compute it in the view, with a conditional for the empty case.
- **denominator** — every count says what it is out of ("56 of 7,693 lines").
- **provenance** — a page with business numbers says where they came from and as of when.
- **absent-is-not-zero** — a missing value renders as a dash or "not measured", never 0.
- **measured-vs-modeled** — an estimate is labelled as one and never summed with a measurement.

## Decisions

- **gated-apply** — a bulk or destructive action is reviewed, then applied.
- **scope-guard** — the apply refuses when the count moved between the look and the click, and
  records the refusal.
- **revertable** — a decision can be undone, and the page says which ones can.
- **confirm-explains** — the confirmation states the consequence; never `window.confirm()`.

## Liveness

- **live-surface** (MUST) — a surface that can change updates itself; no manual refresh.
- **poll-pauses** (MUST) — polling stops while the tab is hidden and re-syncs on return.
- **honest-poll** — "nothing changed" costs a 204, not a re-render.
- **progress-names-domain** — progress is counted in the process's own terms.
- **stream-teardown** — a stream can be cancelled, and the server tears the upstream down.

## Evidence

- **audit-trail** (MUST) — every change writes an append-only row: actor, target, time.
- **evidence-chips** — a finding shows the values that produced it, beside it.
- **method-page** — a page states how each number is counted.
- **no-fabrication** (MUST) — nothing invented renders as real; sample data is labelled.

## Degraded states

- **empty-state** (MUST) — an empty result is a sentence and the next action.
- **source-down** — an unreachable source serves last-good data and says so.
- **timeout-everywhere** (MUST) — every outbound call carries an explicit timeout.
- **ai-degrades** — a model surface labels its lane and degrades to rules, never to an error page.

## Craft

- **design-system** (MUST) — colour, space and type are tokens, never hex in templates.
- **dark-mode** (MUST) — both themes first-class, chosen before first paint.
- **keyboard** — focus-visible styling, aria, keyboard reach.
- **shell-capabilities** — the shell's capabilities are declared (`APP_SHELL`).
- **motion-consent** — motion stops under `prefers-reduced-motion`.
- **status-not-colour** — a status carries a word or glyph as well as a colour.
- **reachable** — if `<body>` is pinned, `<main>` is the declared scroller.
- **responsive** — layout adapts; no fixed desktop width.

## Proof

- **error-visibility** (MUST) — server errors and background-job deaths are readable from outside
  the host: forwarded to the hub's error stream (the hub's `patterns/error-visibility.md`).
- **auth-posture** (MUST) — a request-path gate denies by default, and its arming flag does not
  default off.
- **auth-deploy-verified** — the deploy asserts the anonymous root is refused
  (`service_runner.py --verify`).
- **health-endpoints** (MUST) — `/health/live/`, and a `/health/ready/` that fails on an
  unmigrated database (`SELECT 1` is not readiness).

## Assistant (applies only to an app with a tool registry)

- **agent-scope-declared** (MUST) — tools are registered with their scope; the registry enforces it.
- **agent-write-needs-a-person** (MUST) — a tool that changes something needs a confirmation the
  model cannot mint.
- **agent-payload-allowlist** — returned fields are chosen by inclusion.
- **agent-caps-declared** — a bounded result says what it is out of.
- **agent-tool-timeout** — a tool call is bounded (applies once a model drives the registry).
- **agent-shows-its-work** — the answer arrives with what it looked up (ditto).
- **agent-canon-accounted** (MUST) — every canon family is covered or declared as a gap with a
  reason (`python -m assistant.audit`).
- **agent-canon-current** — the canon counted against is the kit's current one.

## Composable surface (applies only to a dashboard a person arranges)

- **layout-is-placed** (MUST) — a card has a column and a row, not a place in a flow.
- **layout-bounded-server-side** (MUST) — a posted layout is clamped in Python.
- **card-and-drill-agree** — a card's number and its drill rows share one filter.
- **layout-survives-a-grid-change** — an older layout still opens.
- **card-catalog-is-one-place** — one catalog knows every card.

## How the audit keeps itself honest

- **Three-state applicability.** A capability rule answers *does not apply*, *applies*, or — when
  the classifier sees half a capability — *fails loudly*. A wrong "not applicable" is invisible
  in a score, so it is never the quiet default.
- **Vendored code is not the app's.** The assistant kit's own source is read for the rules that
  need it and excluded from the app's code, so a kit's docstring can never be mistaken for the
  app's registry.
- **Declarations are printed.** An app's `.appkit.json` may declare where the rest of it lives
  (`renders_from`, `kits_from`); every declaration is echoed beside the score, honoured or refused.
- **A run that measured nothing fails** (exit 2), and every report names the tree it read and
  whether that checkout is behind its upstream.
- **The bar is audited too.** `app_audit.py --rules <app> <app> ...` shows, per rule, how many apps
  it applies to and how many pass: a rule that passes everyone is a regression guard, one that
  describes almost nobody is an aspiration or an invention, one that never applies is unmeasured.

## What the bar leaves to the real operation

The audit reads source text; it is a map of where to look, never proof the app works. The proof is
the operation itself — sign in, click the button, watch the page — and a transient probe only at a
critical boundary (authorization, destructive data, migrations), run once, its receipt kept, the
probe deleted. That is why there is no rule that rewards a standing test suite: a test written to
confirm code its author just wrote restates the author's belief, and a suite that grows by habit
teaches everyone to read "passed" as "works".
