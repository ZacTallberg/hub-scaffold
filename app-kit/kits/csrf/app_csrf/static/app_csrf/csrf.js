/* The CSRF token a write sends: the cookie, by its CONFIGURED name, read at SEND time --
 * then the token the page rendered.
 *
 * Two failures, one reader:
 *  1. A reader that asks for a cookie called "csrftoken" (or matches any "*csrftoken") finds
 *     nothing once the cookie is renamed, or finds ANOTHER app's token on a host that serves
 *     several apps. The name therefore comes from the page (data-csrf-cookie, rendered from
 *     settings.CSRF_COOKIE_NAME by app_csrf.context_processors.csrf_cookie), never inline.
 *  2. A token baked into the page at render goes STALE: Django rotates it at sign-in, so a tab
 *     that is never reloaded -- a dashboard, a board -- answers 403 on every write while the page
 *     looks alive. The cookie is read when the request is SENT; the rendered token is only the
 *     fallback for the first write, before any cookie exists, or when the cookie is unreadable.
 *
 *   <body data-csrf-cookie="{{ csrf_cookie_name }}" data-csrf="{{ csrf_token }}">
 *   <script src="{% static 'app_csrf/csrf.js' %}" defer></script>
 *
 * Exposes window.appCsrfToken() for fetch() callers and attaches X-CSRFToken to every htmx
 * request automatically.
 */
(function () {
  function cookieValue(name) {
    if (!name) return "";
    var parts = document.cookie ? document.cookie.split(";") : [];
    for (var i = 0; i < parts.length; i++) {
      var pair = parts[i].trim();
      var eq = pair.indexOf("=");
      // An EXACT name match: a suffix or prefix match can hand this page another app's token.
      if (eq > 0 && pair.slice(0, eq) === name) return decodeURIComponent(pair.slice(eq + 1));
    }
    return "";
  }

  function csrfToken() {
    var data = (document.body && document.body.dataset) || {};
    var fromCookie = cookieValue(data.csrfCookie || "");
    if (fromCookie) return fromCookie;
    if (data.csrf) return data.csrf;
    var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
    if (input && input.value) return input.value;
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") || "" : "";
  }
  window.appCsrfToken = csrfToken;

  function attach() {
    document.body.addEventListener("htmx:configRequest", function (event) {
      var token = csrfToken();
      if (token) event.detail.headers["X-CSRFToken"] = token;
    });
  }
  if (document.body) attach(); else document.addEventListener("DOMContentLoaded", attach);
})();
