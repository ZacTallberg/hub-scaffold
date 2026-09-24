/* Keep the lines table current without a manual refresh -- honestly.
 * Every 15s (and at once when the tab becomes visible again, or the live stream says the state
 * changed) ask the server for the rows, sending the fingerprint of what is on screen. The server
 * answers 204 when nothing changed, so an idle table costs one tiny request, not a re-render.
 * Nothing is fetched while the tab is hidden. */
(function () {
  "use strict";
  const body = document.getElementById("line-rows");
  if (!body) return;
  let inflight = null;
  async function sync() {
    if (document.hidden || inflight) return;
    inflight = new AbortController();
    const timer = window.setTimeout(function () { inflight && inflight.abort(); }, 10000);
    try {
      const url = body.dataset.poll + "?fp=" + encodeURIComponent(body.dataset.fp || "");
      const r = await fetch(url, { credentials: "same-origin", signal: inflight.signal,
                                   headers: { "X-Requested-With": "fetch" } });
      if (r.status === 204) return;
      if (!r.ok) throw new Error("HTTP " + r.status);
      body.innerHTML = await r.text();
      body.dataset.fp = r.headers.get("X-FP") || "";
      document.dispatchEvent(new CustomEvent("app-data-changed"));
    } catch (e) {
      if (e.name !== "AbortError" && window.AppShell && window.AppShell.toast) {
        window.AppShell.toast("The table could not refresh; it shows the last good state.", "warn");
      }
    } finally {
      window.clearTimeout(timer);
      inflight = null;
    }
  }
  window.setInterval(sync, 15000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) sync(); });
  document.body.addEventListener("app-refresh", sync);
})();
