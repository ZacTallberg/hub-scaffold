# Pattern: one shared app banner, linked from the hub

When several apps sit behind one hub, each one used to grow its own header: its own page tabs,
its own theme toggle, its own account menu, its own "about" page. They drift the day they are
written. The shared banner is ONE header every adopting app **links** from the hub
(`hub_core/components/banner/`, served at `/hub/components/banner/*`) — never copies — so a fix
to the master reaches every adopter on its next page load, with no app deploy.

The runnable reference is the example site: `DEBUG=1`, then open `/demo/budget-app/` and
`/demo/reporting/` (`?nav=sidebar` for the sidebar rendering). `example/example_site/demo.py`
is a complete adopting app, including a reference implementation of the access contract below.

## Adopt it

```html
<link rel="stylesheet" href="/hub/components/banner/banner.css">
<body data-csrf="{{ csrf_token }}">
<header id="app-banner" data-app="budget-app" data-app-name="Budget" data-app-sub="Forecasts and actuals"
        data-version="2.3.1" data-actor="Alice Example" data-actor-sub="alice" data-role="Admin"
        data-signout="/logout/" data-access-url="/access.json">
  <script type="application/json" data-ab="nav">[
    {"label": "Overview", "href": "/", "icon": "home", "current": true,
     "hint": "Where the month stands against forecast."},
    {"label": "Reports", "icon": "file", "hint": "The packs this app produces.", "children": [
      {"label": "Monthly pack", "href": "/reports/monthly", "hint": "The pack for the monthly review."}]}
  ]</script>
  <div data-ab-slot> <!-- the app's own controls --> </div>
</header>
<script src="/hub/components/banner/banner.js" defer></script>
```

`hub_core/components/banner/manifest.json` lists every mount attribute, island and published
property; `GET /hub/components/` lists each hosted component with a version **measured** from
the bytes being served. The component route serves presentation only (CSS, JS, JSON, SVG, PNG
by an allow-list of suffixes; no separators, no dotfiles, nothing outside the component's own
directory) — no board data and no person's data ever travel through it.

## What it gives every adopter

**Page navigation, declared once.** The `nav` island is `[{label, href, icon, hint, sep, current,
count, countId, children}]`. `data-nav-style="strip"` (default) draws a strip under the band;
`"sidebar"` draws a rail down the left. A `children` list is a dropdown on the strip — it opens on
**hover with intent** (140 ms rest, 260 ms grace to cross into it) and a **click pins it** open,
so the person who clicks to open does not watch it vanish — and a fold in the sidebar. Dropdowns
are positioned against the viewport while open, because the strip scrolls sideways and a
horizontal scroller clips its other axis too. The sidebar collapses to a rail (Ctrl+B, no-op in a
field); on the rail a fold opens as a flyout on hover **and** focus, and nothing is
`display:none`, so Tab still reaches every page. Its open/rail state is the person's, per app.
`count`/`countId` draw a pill the app's own script keeps updating by id; zero is hidden. An item
with no `hint` is reported once in the console.

**Preferences that follow the person.** Theme, size (zoom), text size, reading face, motion, and
the two help switches, held at the hub (`/hub/api/profile`, below) so a choice made in one app is
there in every app and on the next machine. A change **previews** on the page, and the scope is
asked **after** the change: *Save for all apps* or *Only in <this app>*. A row this app overrides
says so and can be put back on the everywhere-value on its own. localStorage only paints the first
frame without a flash. Published on `<html>` for the app's own CSS: `data-theme` (unless the mount
says `data-theme-external`), `data-ab-font`, `data-ab-motion`, `data-ab-tips`, `data-ab-help`, and
`--ab-zoom` (zoom scales viewport units too; divide `vh` by it in fixed layouts).

**Your apps.** The apps this person can reach, starred first, from the hub's join of the
adopter's grants with its app directory. Stars live on the person's record. A grant for an app
with no address is not a row (a row is a door) but is counted, so the omission is visible.

**One drawer from the avatar,** under the band and over the page strip, in the order a person
needs it: who you are (name — role, *Change icon*, *Preferences*), your starred apps, the app's
`profile` island, an **Admins only** section (the `admin` island and People and Permissions),
then a pinned foot — **How this works** (guided tour, About this app, the `help` island), the
super-admin **View as** card, and **Sign out** (always a POST). Four grounds say whose a row is:
the app's, the hub's, admin, help. Roles come from `data-role` ("admin" anywhere in it is an admin,
"super admin" a super admin); the band only **shows** by role — every rule is the server's.

**The person's mark.** Initials, or a picture cropped in the page (drag, wheel or slider zoom
about the pointer, arrow keys, +/-) and saved as a ~96 px JPEG data URL on their record. No
upload lane, no file store; SVG is refused because it can carry script.

**Info bubbles.** Anything on the page with `data-ab-info="what this is for"` (and optional
`data-ab-info-title`) explains itself after a short pause on hover or keyboard focus, placed
below/above/beside to fit the window. Every bubble carries **Hide all info bubbles**, which turns
them off everywhere at once (clearing this app's own override too) and offers **Undo**. A tap is a
press, never a hover. `data-ab-tip` is the instant name chip for glyph-only controls;
`data-ab-help` marks an app's help buttons so the person's *Help buttons: Off* hides them.

**A derived tour and read-me.** Both are built when OPENED from what is on screen: the band's
parts that are visible, then the app's `data-ab-info` key things. Renaming a page or dropping a
control updates them with nobody maintaining a copy. An app that writes its own still wins —
`data-tour-url` → `{steps:[{selector, title, text}]}` (a step whose selector finds nothing is
named in the console and skipped) and `data-readme-url` → `{lead, sections:[{head, lines}], note}`.
The tour restarts rather than layers; the read-me box is `[hidden]` until opened, enforced with
`!important`, because an invisible-but-present box swallows every click and wheel beneath it.

## The People and Permissions contract (`data-access-url`)

The drawer's row opens the whole access page as a box over the app. The component knows this
contract, never the app:

| Request | Answer |
|---|---|
| `GET` | `{roles:[{value,label,what}], default_role, people:[{id, username, display, role, active, self, added_by}], events:[{when, actor, action, target}]}` |
| `GET ?q=<text>` | `{people:[{username, display, subtitle, known}]}`, or `{unconfigured:true}` / `{error}` |
| `POST {action:"add", people:[{username, display}], role}` | `{ok, outcomes:{added, updated, unchanged, refused}}` |
| `POST {action:"set", id, role}` · `{action:"active", id, active}` · `{action:"delete", id}` | `{ok, detail}` or a refusal `{reason}` with a 4xx |

Optional parts (`events`, `what`, search) are drawn only when they come back. One line of people,
**one** role for all of them, one submit. Remove takes two presses and names Deactivate as the
reversible way. The box greys the viewer's own row as a courtesy, but **every guard is the
server's** — self-demotion, self-removal and the last-admin rule — and its refusal is shown
verbatim. POSTs carry `X-CSRFToken`; a refusal that is not JSON still names its HTTP status.

## The person's record: `/hub/api/profile`

This is the same route, engine and store the hub's other app services use
(`patterns/app-services.md`): `hub_core/profiles.py` validates every key against one table (a
value outside its set is **dropped**, never clamped, and named in `ignored`) and stores the record
in `HUB_DIR/profiles.json` — mutable per-person state, never the ledger. A change is POSTed as
`{"prefs": {...}}` and merged per key; the agent component's placement (`agent`) sits in the same
record, so the banner saving a theme never loses it. `prefs.apps: {slug: {...}}` holds per-app
overrides through the same table; an empty override removes the app, a key sent as `null` puts
that one key back; `?app=<slug>` answers `resolved`. Two ways a request may name the person:

- **The app's server**, holding a hub credential, names the person it already signed in:
  `?person=<username>` with `profile:read` / `profile:write`. Also the CLI verb
  (`python -m hub_core.client profile --person alice [--app budget-app] [--set key=value] [--star slug]`)
  and the MCP tool `person_profile`.
- **The person's own browser**, same origin, when the adopter names `HUB_PERSON` — a callable
  `(request) -> username` behind its own sign-in. A browser can never name somebody else, and a
  POST needs the CSRF token. Everyone else gets 404.

"What can I reach" is `HUB_APPS` (the directory: `[{slug, name, url}]`) joined with `HUB_REACH`
(a callable `(person) -> {slug: role}`, `hub_core/reach.py`). The join lists whatever the seam
hands it, so the seam carries two duties the hub cannot check:

- **Active grants only.** Filter revoked, expired and inactive grants at the source; a row in
  the drawer is a door, and a door to an app the person was removed from is a wrong answer.
- **Every spelling of the person.** The hub asks with one lower-cased username; access systems
  often file grants under a short name, a user-principal name (`alice@example.com`) or both.
  Match all of them, or the grants under the other spelling silently vanish from the drawer.

Unset or failing: nothing listed.

## Things this learned the hard way

- **CSRF on a shared host.** Several apps on one host each rename their cookie
  (`budget_app_csrftoken`) and, with no cookie path, all of them sit at `/` on every page. Reading
  "the last cookie ending in csrftoken" sends another app's secret. The banner reads `<body
  data-csrf>`, then a rendered `csrfmiddlewaretoken`, then the cookie **by this app's own name**,
  then Django's default, and only then any suffix match.
- **Glyphs versus host resets.** Every `<svg>` carries its size, fill and stroke as presentation
  attributes, re-asserted with `!important` on an enumerated class, so a host's `svg { fill:
  currentColor }` cannot turn outlines into blobs. One stroke icon set for everything.
- **Placement under zoom.** With `zoom` on `<html>`, rectangles are in zoomed pixels and fixed
  `left/top` are zoomed again; every anchor placement divides by the zoom or lands 10% off.
- **An "empty" verdict must not be a one-way door.** The slot is re-measured with its own hidden
  state lifted, and again whenever its content changes — an app that cloaks its shell until its
  script boots reads as empty on the first paint.
- **Read the theme the page is rendering** (attribute, else the media query), not the stored
  preference, or a toggle labelled "Dark" does nothing on a page that is already dark.
- **Declare before you append.** Every body-level surface is created before the one place that
  appends them; appending a `var` before its value exists throws and silently stops the rest of
  the script at load.
