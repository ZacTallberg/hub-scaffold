# error-visibility — what your app must supply

## Settings

- `APP_SLUG` -- the app's own slug; rows are attributed to it on the hub.
- `HUB_API_BASE` -- the served hub URL (e.g. `https://hub.example.com/hub`); unset keeps the kit local-only.
- `HUB_AGENT_TOKEN` -- a scoped credential holding `error:report`, issued with a lifetime; never the root token.
- `HUB_CA_BUNDLE` -- optional; a private CA bundle (TLS verification stays on).
- `APP_ERRORS_HUB_BRIDGE_PATHS` -- optional; routes that proxy the hub itself.
- `APP_ERRORS_BASE_TEMPLATE` -- optional; render `/errors/` inside your own shell.

## Libraries

- `asgiref` -- installed with Django; a record made from inside a running event loop hops to a worker thread through it.

## Wiring

- `app_errors` in `INSTALLED_APPS`, then `manage.py migrate app_errors`.
- `path("errors/", include("app_errors.urls"))` in the root URLconf, behind the app's own sign-in (it shows stack traces).
- `app_errors/report_errors.js` in the `<head>` of every page (the kit's own `base.html` already does this).
