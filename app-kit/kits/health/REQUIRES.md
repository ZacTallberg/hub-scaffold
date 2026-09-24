# health — what your app must supply

## Wiring

- `app_health` in `INSTALLED_APPS` (the settings kit adds it).
- `path("health/", include("app_health.urls"))` in the root URLconf.
- `health.py` (the drop-in pair) optionally reads `app_errors` -- the error-visibility kit -- to report the forwarder's armed/delivered state as a mode; without it the check reads "kit not installed".
