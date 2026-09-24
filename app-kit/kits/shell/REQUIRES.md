# shell — what your app must supply

## Settings

- `APP_SHELL` -- the declared capabilities (a dict; empty means core only).
- `app_shell.context_processors.shell` in the template context processors (the settings kit adds it).

## From other kits

- `app_gate:logout` -- the account menu posts to the gate kit's sign-out URL when a person is signed in.

## Served by your app, only when declared

- `palette` -- a JSON endpoint returning `{"groups": [{"label": ..., "items": [{"label": ..., "sub": ..., "url": ...}]}]}`, named by `url_name`.
- `live` -- an SSE endpoint (named by `url_name`) emitting `build` (the running build id) and optionally `refresh` (JSON, re-dispatched as an `app-refresh` DOM event). The status word comes from the connection itself: Live, Reconnecting, Offline.
