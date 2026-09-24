# App search bar and header takeover — two linked components

Both live in `hub_core/components/<name>/` beside the shared banner and are served by the hub's
shared-component route (`GET /hub/components/<name>/<file>`, presentation only). Apps LINK them;
a copy is a fork the day it is made. Each `manifest.json` is the full contract.

## Search bar (`components/search-bar/`)

One search box for any app, degrading by configuration rather than by settings:

| The app declares | The reader gets |
|---|---|
| `data-action` only | a complete box that submits to the app's own results page |
| `+ data-suggest-url` | suggest-as-you-type from the app's own endpoint |
| `+ data-chose-url` | the app learns from what people actually OPENED (never from what they typed) |
| `smart: true` in the suggest answer | a second, re-ranked pass the APP computes (`smart=1`) |

The hub serves the file and never an app's data: every URL on the mount is on the app's origin,
behind its sign-in. The component never calls a model and never writes a reason of its own —
`top` and its `why` are the app's. Empty, unconfigured and failed are drawn as three different
states, and a suggest endpoint that answers HTML (a sign-in or error page) is a failure, never
"nothing matched". Inside the banner's `[data-ab-slot]` it inherits the band's tokens.

## Header takeover (`components/header-takeover/`)

A companion to the banner for apps that already had a header when they adopted it:

- `data-replaces="<selector>"` on `#app-banner` hides (never removes) the named header and names
  the reachable controls that went dark with it; `data-replaces=""` means nothing to replace.
- With `data-replaces` absent, a visible header near the top holding controls is WARNED about;
  nothing is hidden. A detector's failure mode must be doing nothing.
- `data-ab-superseded` on an app element hides it once the banner has really drawn.
- An app control in the banner's slot that does what the banner is drawing right now (settings,
  your apps, sign out, the tour, the theme, search) is hidden, checked per control and asked twice.
  `data-ab-keep="<why>"` keeps one and prints the reason. An app's own search box is kept while
  the standard search bar is not mounted.
- The drawer keeps ONE Guided tour, ONE About this app and ONE People and Permissions: an app's
  `profile` / `admin` / `help` island row that repeats a row the banner draws is hidden (a repeated
  app row with no banner counterpart keeps its first copy), and a section left empty loses its
  heading. It re-runs on every drawer rebuild.
- `hide_custom` (a component property an operator sets with `POST /hub/api/component-props`,
  `client component-props --app <slug> --set 'header-takeover.hide_custom=["Export"]'`, or the MCP tool
  `set_component_props`) takes an app's own control off the band, reversibly, scoped to the slot.
  Pages read it from `GET /hub/components/props/<slug>.json` via `data-props` on the mount.

Every outcome is a console line and an `app-banner:duplicate` event, so an app's error visibility
can carry it.

## Proof

Load an adopting page in a real browser with the banner and these files linked, and read what
happened: which elements carry `data-ab-replaced` / `data-ab-duplicate` / `data-ab-offband`, what
the console printed, and what the panel draws for a query that matches, one that matches nothing,
one the app has not wired, and one whose endpoint is down.
