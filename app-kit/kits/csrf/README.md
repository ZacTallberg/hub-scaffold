# csrf (`app_csrf`) — the token a long-lived page sends, read at send time

A page that is never reloaded (a dashboard, a board) goes stale the moment the user signs in again:
Django rotates the CSRF secret at login, so a token baked in at render answers 403 on every write
while the page looks alive. A reader that looks the cookie up by a guessed name (`csrftoken`, the
app's slug, a wildcard) finds nothing after a rename, or a sibling app's token on a shared host.

So the NAME comes from settings, rendered by the page, and the VALUE is read when the request is
sent; the rendered token is the fallback for the first write.

```python
INSTALLED_APPS += ["app_csrf"]
TEMPLATES[0]["OPTIONS"]["context_processors"] += ["app_csrf.context_processors.csrf_cookie"]
```

```html
<body data-csrf-cookie="{{ csrf_cookie_name }}" data-csrf="{{ csrf_token }}">
<script src="{% static 'app_csrf/csrf.js' %}" defer></script>
```

Every htmx request then carries `X-CSRFToken`; `fetch` callers use `window.appCsrfToken()`. With
`CSRF_USE_SESSIONS` or `CSRF_COOKIE_HTTPONLY` the rendered name is empty and the reader uses the
rendered token. The Hub board applies the same rule (`<meta name="csrf-cookie">`).
