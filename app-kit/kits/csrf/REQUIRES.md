# csrf — what your app must supply

## Settings

- `CSRF_COOKIE_NAME` -- read, never guessed; with `CSRF_USE_SESSIONS` or `CSRF_COOKIE_HTTPONLY` the page falls back to the rendered token.

## Wiring

- `app_csrf` in `INSTALLED_APPS`.
- `app_csrf.context_processors.csrf_cookie` in the template context processors.
- `data-csrf-cookie="{{ csrf_cookie_name }}"` and `data-csrf="{{ csrf_token }}"` on `<body>`, and `app_csrf/csrf.js` loaded on every page that writes.
