# health — what your app must supply

## Wiring

- `app_health` in `INSTALLED_APPS` (the settings kit adds it).
- `path("health/", include("app_health.urls"))` in the root URLconf.
