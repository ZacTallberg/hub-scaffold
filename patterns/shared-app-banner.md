# A shared app banner — linked from one master, never copied

When one operator runs many small apps, every app grows its own header, and they drift: one has
a connection light, one has sign-out as a GET link, one lost its theme switch. The fix that holds
is a **shared banner component**: one JavaScript + CSS pair, hosted once (the Hub is a natural
host), that every app LINKS. A change to the master reaches every adopting app on its next page
load with no app deploy. This is the contract such a component must keep; the markup, brand and
feature set are yours.

Register it in the Hub as a standard component so new apps find it and skeletons apply it:

```bash
python -m hub_core.client capability --kind component --name "App banner" \
  --delivery hosted --hosted-at https://hub.example.com/components/banner/ \
  --what "Brand, navigation, connection status, notices, settings and sign-out for any app" \
  --when "Every browser-facing app; decline a part with data-omit rather than forking it" \
  --get "Link banner.css in <head>; place <div id=app-banner data-app=...> then banner.js right after it" \
  --entry "templates/base.html" --default
```

## Mount by data, not by code

- **One mount element with data attributes** (`data-app` — required, it keys per-app state —
  `data-app-name`, `data-home`, `data-actor`, `data-signout`, `data-health-url`, ...), plus
  optional JSON islands (`<script type="application/json" data-banner="nav">`) for lists. An app
  configures; it never edits the component.
- **Slots, not overrides.** An app's own controls go in a declared slot
  (`<div data-banner-slot="custom">`) that the component moves into place. Lay the slot out
  explicitly (a plain `<div>` is not a row) and re-measure it after load, because an app that
  hides its shell until its own script boots would otherwise read as empty forever.
- **Decline a part, don't fork it:** `data-omit="theme,settings"` for an app that already owns a
  richer version of one of them.
- **A fallback that disappears.** Anything marked `[data-banner-fallback]` is what a reader sees
  if the component never loads (the hub is down); the component removes it only once it has
  rendered.

## Hide, never remove — and never by guess

The banner often stands in for an app's older header. Hide the replaced elements (the app
declares them with `data-replaces="<selector>"`) only after the banner is really in the DOM, and
**never remove** them: an app binds its own controls by id at startup, and a deleted node throws.
If the app declares nothing, WARN in the console and hide nothing — a detector whose failure mode
is "remove a capability" is worse than the duplicate it fixes. When the component hides a
duplicate it is really drawing (a second bell, a second status light), let the app keep one on
purpose with a reason (`data-banner-keep="why"`) and print that reason in the component's report.

## The parts that are easy to get wrong

- **Connection status: red needs two misses, or the browser saying it is offline.** One slow
  answer on a busy host is not an outage; a light that flickers red trains people to ignore it.
  The first miss re-checks after a few seconds, the second turns it red; the `offline` event turns
  it red at once because it is not a guess; `online` re-checks immediately. Probe the app's own
  unauthenticated liveness endpoint, never a gated one.
- **Sign-out is a POST form with the CSRF token**, never a GET link — a GET sign-out can be fired
  by any image tag on any page.
- **Find the CSRF token in a fixed order:** a `data-csrf` attribute the page renders (the only
  source that survives `HttpOnly` cookies and cannot be renamed out from under you), then a
  rendered form token, then the cookie — the app's OWN cookie name first, the framework default
  next, and a suffix match last. Apps on one host commonly share a cookie path, so a reader who has
  opened three apps carries three `*_csrftoken` cookies; a suffix match that keeps the last one
  sends another app's secret, and the save fails 403 on a page that renders perfectly.
- **Surfaces appended to `<body>` (menus, drawers, tooltips) must declare the tokens they paint
  with** on their own root as well as the banner's, because they are outside the banner's
  subtree and inherit nothing from it.
- **Theme:** if the app's own script binds the theme control by id, let it
  (`data-theme-external`); the component draws the control and binds nothing.
- **Honor the reader:** reduced motion disables hover glows and transitions; help affordances can
  be hidden by preference (hidden, not removed, so bound scripts still find them).

## Proving a change

The master reaches every adopter on its next load, so a change to it is a change to every app at
once. Prove it the way `docs/TESTING.md` says: load two real adopting pages (one light, one dark,
one narrow) and look at them; there is no standing visual suite. Forward the component's own browser
failures to the Hub through each app's error forwarder (`patterns/error-visibility.md`, or
`python -m hub_core.client app-error`) so a broken master is visible before the first person
reports it.
