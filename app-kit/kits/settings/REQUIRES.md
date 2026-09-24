# settings — what your app must supply

## Arguments to `configure()`

- `app_module` -- your Django app's package name.
- `root_urlconf` -- your URLconf module.
- `gate_table_prefix` -- a committed lowercase identifier for this app's gate tables.

## Environment (production; `python envcheck.py .env` names every one that is missing)

- `APP_NAME` -- display name.
- `APP_SLUG` -- lowercase letters, digits, hyphens.
- `DJANGO_SECRET_KEY` -- at least 50 characters, not a placeholder.
- `DJANGO_ALLOWED_HOSTS` -- explicit hosts, including 127.0.0.1 and localhost.
- `DJANGO_DEBUG` -- `false`.
- `APP_GATE_REQUIRED` -- `true`, which then requires `APP_GATE_SUPERADMINS`.

## Wiring

- `appkit_settings.static_urlpatterns` -- add `*appkit_settings.static_urlpatterns()` to the root URLconf: a production WSGI server serves no static files, so without it the shell ships with no stylesheet and no script. Run `manage.py collectstatic` on deploy, or set `APP_SERVE_STATIC=false` when a proxy serves `STATIC_ROOT`.
