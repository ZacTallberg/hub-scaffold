# gate — what your app must supply

## Settings

- `APP_GATE_TABLE_PREFIX` -- this app's own lowercase identifier, committed in settings (the settings kit's `gate_table_prefix`). The model module refuses to import without one.
- `APP_GATE_SUPERADMINS` -- a LIST of owner usernames.
- `APP_GATE_REQUIRED` -- `True` in production (the settings kit defaults it on).
- `APP_GATE_AUTHENTICATOR` -- optional; `module:callable` for a directory bind.
- `APP_GATE_PUBLIC_PATHS` / `APP_GATE_PUBLIC_PREFIXES` -- optional; each public path named on purpose.

## Wiring

- `app_gate.middleware.GateMiddleware` after `AuthenticationMiddleware`.
- `path("auth/", include("app_gate.urls"))` in the root URLconf.
- `app_gate.context_processors.actor` for templates that show who is signed in.
