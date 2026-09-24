# shell — the app chrome, with capabilities an app DECLARES

`app_shell/base.html` + `shell.js` + `shell.css`. Every capability is one implementation, armed
only when the app declares it:

```python
APP_SHELL = {
    "toasts": True,                        # transient confirmations
    "palette": {"url_name": "app:palette"},  # Ctrl/Cmd-K over an index this app serves
    "live": {"url_name": "app:events"},     # SSE: status in words + "a newer version is live"
    "drawer": True,                        # right-hand record detail ([data-drawer-url])
    "confirm": True,                       # data-confirm="<the consequence>"
    "transitions": True,                   # view transitions on htmx swaps
    "prefetch": True,                      # warm drill targets on INTENT only
}
```

Anything not named is inert: no markup, no listener, no fetch. A shell that carries every
capability as always-on code decays into furniture nobody can justify; one whose capabilities are
declared is one an audit can read (`app_shell.W001` warns about an unknown key).

## Always on, because they are the difference between usable and merely rendered

- **Theme before first paint** — `data-theme` is set in `<head>`, both themes are tokens.
- **One scroller.** `<body>` is pinned so the header stays; `<main class="app-scroll">` scrolls,
  and the shell answers PageDown/Home/End/Space on it. A browser only scrolls a region that holds
  focus, and a page opens with focus on `<body>` — without this, PageDown moves nothing.
- **Polls pause while the tab is hidden**, and the live stream closes; both resume on return.
- **No native `confirm()`** — its chrome shows the origin, it can be suppressed by the browser's
  "prevent additional dialogs" box (turning a guarded action unguarded), and it has no room for
  what the click is about to DO.

## Prefetch warms on intent, never ahead

`prefetch.js` warms a drill target or a link after a 150 ms hover or on keyboard focus — one at a
time, never while a request a person is waiting for is in flight, into bounded caches cleared by an
`app-data-changed` event, and not at all when the tab is hidden or the connection asks to save
data. Warming every row on a grid queues hundreds of requests on a single-executor server and makes
a real save wait behind the warm-up. Warm-up requests carry `X-Prefetch: 1`.
