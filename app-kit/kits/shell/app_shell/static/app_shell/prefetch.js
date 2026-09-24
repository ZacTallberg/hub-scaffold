/* Intent prefetch — warm a drill target or a page on INTENT, never ahead of a real click.
 *
 * Warming the thing a person is about to open makes the first click feel instant. Warming
 * EVERYTHING on the page does the opposite: measured on a 150-row grid with ~3 drill targets per
 * row, a warm-every-target idiom (three at a time, restarted after every swap) queued hundreds of
 * requests on a server that runs its sync views on one executor, so a real inline edit waited
 * behind the warm-up — saves took 7-10 seconds. Warming on intent only, one at a time, behind a
 * shared busy counter, with bounded caches: median 154ms, worst 297ms on the same page.
 *
 * The rules, each load-bearing:
 *   - INTENT only: a 150ms hover, or keyboard focus, on [data-drawer-url] / [data-prefetch].
 *     Never on load, never on idle, never "the rest of the page".
 *   - ONE warm-up at a time, and none while a request a person is waiting for is in flight.
 *   - BOUNDED caches (oldest evicted), cleared by an `app-data-changed` event.
 *   - Nothing when the tab is hidden or the connection asks to save data.
 *   - A failed warm-up is forgotten, so the real click always tries again.
 * Warm-up requests carry `X-Prefetch: 1` so the server can skip side effects and logging.
 */
(function () {
  "use strict";
  if (navigator.connection && navigator.connection.saveData) return;

  const MAX_DRILL = 60;
  const MAX_PAGES = 40;
  const INTENT_DELAY_MS = 150;
  const drill = new Map();          /* url -> Promise<html>, insertion-ordered for eviction */
  const pages = new Set();
  let warming = false;

  /* One count of work a person is waiting for. Every warm-up stands aside while it is > 0. */
  const busy = window.__appBusy = window.__appBusy || { count: 0 };
  document.addEventListener("htmx:beforeRequest", function (event) {
    const config = event.detail && event.detail.requestConfig;
    if (config && !config._appCounted) { config._appCounted = true; busy.count += 1; }
  });
  document.addEventListener("htmx:afterRequest", function (event) {
    const config = event.detail && event.detail.requestConfig;
    if (config && config._appCounted) { config._appCounted = false; busy.count = Math.max(0, busy.count - 1); }
  });

  function blocked() { return warming || busy.count > 0 || document.hidden; }

  function evict(store, max) {
    while (store.size >= max) {
      const oldest = store.keys().next().value;
      store.delete(oldest);
    }
  }

  function warmDrill(url) {
    if (!url || drill.has(url) || blocked()) return;
    evict(drill, MAX_DRILL);
    warming = true;
    const fetchPartial = (window.AppShell && window.AppShell.fetchPartial) || function (u) {
      return fetch(u, { credentials: "same-origin", headers: { "HX-Request": "true", "X-Prefetch": "1" } })
        .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.text(); });
    };
    const pending = fetchPartial(url, undefined, true);
    drill.set(url, pending);
    pending.then(function () { warming = false; }, function () {
      warming = false;
      if (drill.get(url) === pending) drill.delete(url);
    });
  }

  function pageUrl(anchor) {
    if (!anchor || anchor.target || anchor.hasAttribute("download")) return "";
    const url = new URL(anchor.href, window.location.href);
    if (url.origin !== window.location.origin || url.href === window.location.href) return "";
    return url.pathname + url.search;
  }

  function warmPage(url) {
    if (!url || pages.has(url) || pages.size >= MAX_PAGES || blocked()) return;
    warming = true;
    fetch(url, { credentials: "same-origin", headers: { "X-Prefetch": "1" } })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); pages.add(url); })
      .catch(function () { /* a later intent may retry */ })
      .finally(function () { warming = false; });
  }

  function target(event) {
    const el = event.target.closest && event.target.closest("[data-drawer-url], a[data-prefetch]");
    if (!el) return null;
    if (el.hasAttribute("data-drawer-url")) return { kind: "drill", url: el.getAttribute("data-drawer-url") };
    return { kind: "page", url: pageUrl(el) };
  }

  let timer = null, current = null;
  document.addEventListener("mouseover", function (event) {
    const t = target(event);
    const key = t ? t.kind + ":" + t.url : null;
    if (key === current) return;
    current = key;
    window.clearTimeout(timer);
    if (!t) return;
    timer = window.setTimeout(function () {
      if (t.kind === "drill") warmDrill(t.url); else warmPage(t.url);
    }, INTENT_DELAY_MS);
  }, { passive: true });
  document.addEventListener("focusin", function (event) {
    const t = target(event);
    if (t) { if (t.kind === "drill") warmDrill(t.url); else warmPage(t.url); }
  });
  document.addEventListener("app-data-changed", function () { drill.clear(); pages.clear(); });

  window.AppShell = window.AppShell || {};
  window.AppShell.takeWarm = function (url) { return drill.get(url) || null; };
  window.AppShell.prefetchState = function () {
    return { drill: Array.from(drill.keys()), pages: Array.from(pages), busy: busy.count, warming: warming };
  };
})();
