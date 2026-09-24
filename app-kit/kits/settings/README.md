# settings — the fail-closed posture in one call

`appkit_settings.configure(globals(), BASE_DIR, app_module=..., root_urlconf=...,
gate_table_prefix=...)` fills a Django settings module with the app-kit posture;
`envcheck.py` validates a production environment and names **every** problem at once.

## What it decides, and why

| Decision | Why |
|---|---|
| `DEBUG` defaults off; `SECRET_KEY` required outside DEBUG | a missing key must fail, never fall back to a dev value |
| the identity gate defaults **on** | an app is never anonymous by accident |
| `ALLOWED_HOSTS` never defaults to `*` | a wildcard host is a configuration nobody chose |
| one fixed **test posture**, detected before the `.env` is read | a laptop `.env` holding production values cannot turn a suite red that CI runs green |
| `TESTING` is an alias of `RUNNING_TESTS` | code that asks for the other name must not read `False` and disarm its "never touch the live system from a test" guard |
| `gate_table_prefix` is a committed literal | a missing environment key must never be able to rename a live app's tables |
| the model lane (`APP_LLM_BASE_URL`) has **no default host** | an unset lane is OFF and says so (`APP_LLM_LANE`), rather than shipping pointed at a machine nothing guarantees is there; `APP_LLM_REQUIRED=true` makes the validator refuse to boot without it |
| SQLite opens in WAL with `IMMEDIATE` transactions | two simultaneous writers WAIT instead of failing "database is locked" |

## Every problem in one throw

A validator that stops at the first missing key makes each further key cost a whole deploy round
trip. `envcheck` collects missing keys, invalid values and the keys a posture pulls in (gate on →
owners; external database on → its URL; model required → its endpoint) and raises once:

```
python envcheck.py path/to/.env      # exit 0 clean, 1 with every problem listed
```

Value checks run only on keys that are present, so a missing key is reported as missing instead of
also tripping a shape check on an empty string. Extend it with `extra_required=` / `extra_rules=`.
