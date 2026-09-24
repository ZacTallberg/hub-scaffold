/* Search bar -- one search box an app LINKS from the hub (never copies).

   An app names its OWN results page and, optionally, its OWN endpoints; the box degrades by
   configuration rather than by settings:

     data-action alone           a complete search box that submits to the results page
     + data-suggest-url          a panel that suggests as you type
     + data-chose-url            the app learns from what people actually OPENED
     + smart:true in the answer  a second, re-ranked pass the APP computes (smart=1)

     <div id="app-search" data-app="budget-app" data-app-name="Budget"
          data-action="/budget/search/" data-sources="budgets, cost centres, owners"
          data-suggest-url="/budget/search/suggest" data-chose-url="/budget/search/chose"
          data-value="{{ query }}">
       <input data-search-fallback type="search" name="q" placeholder="Search">
     </div>
     <script src="/hub/components/search-bar/search-bar.js"></script>   (NOT deferred; right after the mount)

   THE HUB SERVES THIS FILE AND NEVER AN APP'S DATA. Every URL on the mount is a path on the
   app's own origin, behind the app's own sign-in. This file holds no credential, never calls the
   hub (except an optional `data-props` read of presentation properties) and never calls a model:
   `top`, its `why`, and the smart pass are the app's to compute. The contract is in manifest.json.

   Standard browser APIs only; no framework, no build step. */
(function () {
  "use strict";

  var VERSION = "1.3.0";
  var doc = document, win = window;
  var mount = doc.getElementById("app-search") || doc.querySelector("[data-app-search]");
  if (!mount || mount.getAttribute("data-sb-ready") === "1") return;

  var D = mount.dataset;
  var ACTION = (D.action || "").trim();
  if (!ACTION) return;                     // no results page, no bar: never a control that opens nothing

  mount.setAttribute("data-sb-ready", "1");

  var APP = (D.app || "app").replace(/[^a-z0-9_-]/gi, "-");
  var APP_NAME = D.appName || "";
  var PARAM = D.param || "q";
  var SUGGEST_URL = (D.suggestUrl || "").trim();
  var CHOSE_URL = (D.choseUrl || "").trim();
  var APP_HOTKEY = (D.hotkey || "/").slice(0, 1);
  var MIN_CHARS = parseInt(D.min || "2", 10) || 2;
  var KEY = "app-search:" + APP + ":";
  var RECENT_MAX = 6;

  /* COMPONENT PROPERTIES -- what an operator set for this app's search bar (the pre-text and the
     jump-to-search key), read from window.HubComponentProps at load, from `data-props` if the app
     names the hub's props route, and from the `hub:component-props` event after. Whatever is not
     set leaves the app's own behaviour. */
  var PROPS = {};
  var endpointPlaceholder = "";
  function takeProps(all) {
    var mine = all && (all["search-bar"] || (all.props && all.props["search-bar"]));
    PROPS = (mine && typeof mine === "object") ? mine : {};
  }
  try { if (win.HubComponentProps) takeProps(win.HubComponentProps); } catch (e) {}
  function hotkey() {
    if (PROPS.hotkey === "off") return "";
    return PROPS.hotkey ? String(PROPS.hotkey).slice(0, 1) : APP_HOTKEY;
  }

  /* ------------------------------------------------------------- small helpers */

  // localStorage is a per-viewer convenience only (this reader's recent queries). It throws in a
  // private window and comes back empty with site data cleared, so every touch is guarded.
  function recall(k) { try { return localStorage.getItem(KEY + k) || ""; } catch (e) { return ""; } }
  function remember(k, v) { try { localStorage.setItem(KEY + k, v); } catch (e) {} }
  function recentList() {
    var raw = recall("recent");
    if (!raw) return [];
    try { var v = JSON.parse(raw); return Array.isArray(v) ? v.slice(0, RECENT_MAX) : []; }
    catch (e) { return []; }
  }
  function recentPush(q) {
    q = String(q || "").trim();
    if (!q) return;
    var list = recentList().filter(function (x) { return String(x).toLowerCase() !== q.toLowerCase(); });
    list.unshift(q);
    remember("recent", JSON.stringify(list.slice(0, RECENT_MAX)));
  }
  function el(tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function svg(markup, w) {
    var span = doc.createElement("span");
    span.innerHTML = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true" width="' + w +
      '" height="' + w + '">' + markup + "</svg>";
    return span.firstChild;
  }

  // CSRF from <body data-csrf>, then a csrfmiddlewaretoken input, then a cookie matched by
  // SUFFIX. Apps sharing one host namespace the cookie by their own name ("<app>_csrftoken"), so a
  // shared component that looks only for "csrftoken" sends an empty header and every beacon 403s.
  function csrfToken() {
    var fromBody = (doc.body && doc.body.dataset && doc.body.dataset.csrf) || "";
    if (fromBody) return fromBody;
    var field = doc.querySelector("input[name=csrfmiddlewaretoken]");
    if (field && field.value) return field.value;
    var pairs = (doc.cookie || "").split(";");
    for (var i = 0; i < pairs.length; i++) {
      var bits = pairs[i].split("=");
      var name = (bits[0] || "").trim();
      if (name === "csrftoken" || /(^|_)csrftoken$/.test(name)) {
        return decodeURIComponent((bits.slice(1).join("=") || "").trim());
      }
    }
    return "";
  }

  /* THE PRE-TEXT IS THE APP'S. Best first: an operator's property; `placeholder` from the suggest
     endpoint (the app counting its own corpus -- dynamic AND current); data-placeholder;
     data-sources composed into one sentence so every app's box is phrased alike; the app's name.
     It never rotates, and the endpoint's placeholder is applied ONCE: a pre-text that changes
     under somebody who is deciding what to type is a pre-text nobody trusts. */
  function listPhrase(items) {
    if (!items.length) return "";
    if (items.length === 1) return items[0];
    if (items.length > 4) return items.slice(0, 3).join(", ") + " and more";
    return items.slice(0, -1).join(", ") + " and " + items[items.length - 1];
  }
  function derivedPlaceholder() {
    if (PROPS.placeholder) return String(PROPS.placeholder);
    if (endpointPlaceholder) return endpointPlaceholder;
    if (D.placeholder) return D.placeholder;
    var sources = (D.sources || "").split(",").map(function (s) { return s.trim(); })
      .filter(function (s) { return !!s; });
    if (sources.length) return "Search " + listPhrase(sources) + "…";
    if (APP_NAME) return "Search " + APP_NAME + "…";
    return "Search…";
  }

  /* ------------------------------------------------------------------- the bar */

  // Whatever the app left marked [data-search-fallback] is its no-JS box; it is removed only once
  // this component has built its own, so a hub that never answers leaves a working search behind.
  var fallbacks = mount.querySelectorAll("[data-search-fallback]");

  var form = el("form", "sb");
  form.setAttribute("role", "search");
  form.method = "get";
  form.action = ACTION;

  var label = el("label", "sb-sr", APP_NAME ? "Search " + APP_NAME : "Search");
  label.setAttribute("for", "sb-input-" + APP);

  var icon = el("span", "sb-icon");
  icon.appendChild(svg('<circle cx="11" cy="11" r="6.4" stroke="currentColor"/>' +
    '<path d="m15.8 15.8 3.7 3.7" stroke="currentColor" stroke-linecap="round"/>', 18));

  var input = el("input", "sb-input");
  input.type = "search";
  input.name = PARAM;
  input.id = "sb-input-" + APP;
  input.autocomplete = "off";
  input.value = D.value || "";
  input.placeholder = derivedPlaceholder();
  input.setAttribute("spellcheck", "false");
  // The combobox role ONLY when a panel can exist: announcing a listbox that never appears is a
  // lie told in the accessibility tree.
  if (SUGGEST_URL) {
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-controls", "sb-panel-" + APP);
  }

  var clear = el("button", "sb-clear");
  clear.type = "button";
  clear.setAttribute("aria-label", "Clear the search");
  clear.appendChild(svg('<path d="M6 6l12 12M18 6 6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>', 13));

  // The agent-powered mark: drawn ONLY when the app declares its search is model-assisted
  // (data-agent-powered). A mark claiming a model the app does not have would be a lie.
  var star = null;
  if (D.agentPowered !== undefined && D.agentPowered !== "0" && D.agentPowered !== "false") {
    star = el("span", "sb-agent");
    star.setAttribute("role", "img");
    star.setAttribute("aria-label", "Agent powered");
    star.setAttribute("data-ab-tip", "Agent powered");      // the banner's instant tooltip, when inside it
    if (!mount.closest("#app-banner")) star.title = "Agent powered";
    star.appendChild(svg('<path d="M12 3.2l2 5.3 5.3 2-5.3 2-2 5.3-2-5.3-5.3-2 5.3-2z" stroke="currentColor" ' +
      'stroke-width="1.5" stroke-linejoin="round"/>', 15));
  }

  form.appendChild(label);
  form.appendChild(icon);
  form.appendChild(input);
  form.appendChild(clear);
  if (star) form.appendChild(star);
  mount.appendChild(form);
  for (var f = 0; f < fallbacks.length; f++) {
    if (fallbacks[f].parentNode) fallbacks[f].parentNode.removeChild(fallbacks[f]);
  }

  /* ----------------------------------------------------------------- the panel */

  var panel = null, list = null, active = -1, options = [];
  var placeholderSettled = false;
  var popular = [];

  if (SUGGEST_URL) {
    panel = el("div", "sb-panel");
    panel.id = "sb-panel-" + APP;
    panel.hidden = true;
    list = el("div", "sb-list");
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", "Search suggestions");
    panel.appendChild(list);
    var foot = el("div", "sb-foot");
    foot.appendChild(el("span", null, "↑↓ to move · ↵ to open · Esc to close"));
    panel.appendChild(foot);
    form.appendChild(panel);
  }

  function isOpen() { return !!panel && !panel.hidden; }
  function clearList() {
    if (list) { while (list.firstChild) list.removeChild(list.firstChild); }
    options = [];
    active = -1;
  }
  function openPanel() {
    if (!panel || isOpen()) return;
    panel.hidden = false;
    input.setAttribute("aria-expanded", "true");
  }
  function closePanel() {
    if (!isOpen()) return;
    panel.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    active = -1;
  }
  // Hover and keyboard highlight are ONE state: moving between mouse and arrows never shows two
  // rows claiming to be current.
  function paintActive() {
    for (var i = 0; i < options.length; i++) {
      var on = i === active;
      options[i].node.setAttribute("aria-selected", on ? "true" : "false");
      options[i].node.classList.toggle("is-active", on);
      if (on) {
        input.setAttribute("aria-activedescendant", options[i].node.id);
        if (options[i].node.scrollIntoView) options[i].node.scrollIntoView({ block: "nearest" });
      }
    }
    if (active < 0) input.removeAttribute("aria-activedescendant");
  }

  // EMPTY, UNCONFIGURED AND FAILED ARE THREE FACTS, drawn three ways: a reader who cannot tell
  // "nothing matched" from "this lane is down" stops believing both.
  function drawState(title, body, tone) {
    clearList();
    var wrap = el("div", "sb-state" + (tone ? " sb-state--" + tone : ""));
    wrap.setAttribute("data-sb-state", tone || "plain");
    wrap.appendChild(el("div", "sb-state-title", title));
    if (body) wrap.appendChild(el("div", "sb-state-body", body));
    list.appendChild(wrap);
    openPanel();
  }

  function addOption(row, groupKey, rank, extraCls) {
    var a = el("a", "sb-opt" + (extraCls ? " " + extraCls : ""));
    a.id = "sb-opt-" + APP + "-" + options.length;
    a.setAttribute("role", "option");
    a.setAttribute("aria-selected", "false");
    a.href = row.url || "#";
    var body = el("span", "sb-opt-body");
    body.appendChild(el("span", "sb-opt-title", String(row.title || "")));
    if (row.meta) body.appendChild(el("span", "sb-opt-meta", String(row.meta)));
    a.appendChild(body);
    // The app's own sentence, never the component's: no `why`, no claim.
    if (row.why) a.appendChild(el("span", "sb-opt-why", String(row.why)));
    var entry = { node: a, row: row, group: groupKey, rank: rank };
    a.addEventListener("click", function () { choose(entry); });
    a.addEventListener("mousemove", function () {
      var i = options.indexOf(entry);
      if (i !== active) { active = i; paintActive(); }
    });
    options.push(entry);
    list.appendChild(a);
    return entry;
  }

  /* LEARNING FROM USE. The one thing sent back, and only when a person REACHED something: the
     query, what they opened and where it sat. A query merely typed, or run and abandoned, is not
     evidence. `keepalive` so the beacon survives the navigation it reports; failures swallowed so
     a learning lane that is down can never break a search that works. */
  function choose(entry) {
    var q = input.value.trim();
    recentPush(q);
    if (!CHOSE_URL || !q || entry.rank < 0) return;
    try {
      fetch(CHOSE_URL, {
        method: "POST", credentials: "same-origin", keepalive: true,
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken(),
                   "X-Requested-With": "XMLHttpRequest" },
        body: JSON.stringify({ q: q, ref: entry.row.ref || "", url: entry.row.url || "",
                               kind: entry.group || "", rank: entry.rank })
      }).catch(function () {});
    } catch (e) {}
  }

  function termRow(q, group) {
    return addOption({ title: String(q), url: ACTION + (ACTION.indexOf("?") === -1 ? "?" : "&") +
                       PARAM + "=" + encodeURIComponent(q) }, group, -1, "sb-opt--term");
  }

  // The empty state: this reader's recent searches and the app's popular ones. Learned examples
  // live HERE, readable and clickable, never in a rotating placeholder.
  function drawIdle() {
    if (!list) return;
    clearList();
    var recent = recentList(), any = false;
    if (recent.length) {
      list.appendChild(el("div", "sb-group-head", "Your recent searches"));
      recent.forEach(function (q) { termRow(q, "recent"); });
      any = true;
    }
    if (popular.length) {
      list.appendChild(el("div", "sb-group-head", APP_NAME ? "Popular in " + APP_NAME : "Popular searches"));
      popular.slice(0, 5).forEach(function (q) { termRow(q, "popular"); });
      any = true;
    }
    if (!any) { closePanel(); return; }
    paintActive();
    openPanel();
  }

  /* ---------------------------------------------------------------- suggesting */

  var timer = null, controller = null, lastQuery = null;
  var cache = {};                       // this page's answers only; never stored

  /* THE SMART PASS. The fast answer comes every keystroke. When the APP says a second pass exists
     (`smart: true`), the person typed a real question (3+ words) and paused, the component asks
     again with smart=1 and draws the app's re-ranked answer. Any keystroke cancels; a failure
     leaves the fast answer standing and says so quietly (`smart_error`). */
  var SMART_WORDS = 3, SMART_PAUSE_MS = 650;
  var smartTimer = null, smartController = null;

  function suggestUrl(q, smart) {
    return SUGGEST_URL + (SUGGEST_URL.indexOf("?") === -1 ? "?" : "&") + PARAM + "=" +
      encodeURIComponent(q) + (smart ? "&smart=1" : "");
  }
  function getJSON(url, signal) {
    return fetch(url, { credentials: "same-origin", signal: signal,
                        headers: { "X-Requested-With": "XMLHttpRequest", "Accept": "application/json" } })
      .then(function (r) {
        return r.json().catch(function () {
          // A JSON endpoint that answered HTML is a sign-in or error page. Reading it as "no
          // results" is how a broken lane hides behind a working-looking box.
          return { ok: false, reason: "failed",
                   error: "This app's suggest endpoint answered with something that was not JSON (HTTP " + r.status + ")." };
        });
      });
  }
  function cancelSmart() {
    if (smartTimer) { clearTimeout(smartTimer); smartTimer = null; }
    if (smartController) { try { smartController.abort(); } catch (e) {} smartController = null; }
    var busy = list && list.querySelector(".sb-reading");
    if (busy) busy.parentNode.removeChild(busy);
  }
  function maybeSmart(q, fast) {
    cancelSmart();
    if (!fast || !fast.smart || fast.smart_used) return;
    if (q.split(/\s+/).filter(Boolean).length < SMART_WORDS) return;
    var key = "smart:" + q;
    smartTimer = setTimeout(function () {
      smartTimer = null;
      if (q !== input.value.trim()) return;
      if (cache[key]) { render(cache[key], q); return; }
      var reading = el("div", "sb-reading", "Reading your question…");
      reading.setAttribute("role", "status");
      list.insertBefore(reading, list.firstChild);
      openPanel();
      smartController = ("AbortController" in win) ? new AbortController() : null;
      getJSON(suggestUrl(q, true), smartController ? smartController.signal : undefined).then(function (data) {
        smartController = null;
        if (reading.parentNode) reading.parentNode.removeChild(reading);
        if (q !== input.value.trim() || !data || data.ok === false) return;
        cache[key] = data;
        render(data, q);
      }).catch(function (e) {
        smartController = null;
        if (e && e.name === "AbortError") return;
        if (reading.parentNode) reading.parentNode.removeChild(reading);
      });
    }, SMART_PAUSE_MS);
  }

  function render(data, q) {
    clearList();
    if (!data || data.ok === false) {
      if (data && data.reason === "unconfigured") {
        drawState("Suggestions are not switched on here",
                  data.error || "This app can search, but it has not been wired to suggest as you type. " +
                  "Press ↵ to run the search itself.", "quiet");
      } else {
        drawState("Suggestions could not be loaded",
                  (data && data.error) || "Something went wrong reaching this app's server. " +
                  "Press ↵ to run the search itself — that path is unaffected.", "warn");
      }
      return;
    }
    var groups = Array.isArray(data.groups) ? data.groups : [];
    var total = 0;
    groups.forEach(function (g) { total += ((g && g.rows) || []).length; });
    var hasTop = !!(data.top && data.top.title);
    if (!total && !hasTop) {
      drawState("Nothing matched “" + q + "”", "Try fewer words, or part of a name or a key.", "quiet");
      return;
    }
    // The learned top match, only when the app sent one -- and lifted OUT of its group rather
    // than drawn above a copy of itself.
    if (hasTop) {
      list.appendChild(el("div", "sb-group-head sb-group-head--top", "Most likely what you want"));
      addOption(data.top, data.top.kind || "top", 0, "sb-opt--top");
    }
    var topRef = hasTop ? (data.top.ref || data.top.url || "") : "";
    groups.forEach(function (g) {
      var rows = ((g && g.rows) || []).filter(function (row) {
        return !topRef || (row.ref || row.url || "") !== topRef;
      });
      if (!rows.length) return;
      list.appendChild(el("div", "sb-group-head", String(g.title || g.key || "Results")));
      rows.forEach(function (row, i) { addOption(row, g.key || "", i); });
    });
    if (data.smart_error) list.appendChild(el("div", "sb-note", String(data.smart_error)));
    if (typeof data.total === "number" && data.total > total) {
      var more = el("a", "sb-more", "See all " + data.total + " results");
      more.href = ACTION + (ACTION.indexOf("?") === -1 ? "?" : "&") + PARAM + "=" + encodeURIComponent(q);
      list.appendChild(more);
    }
    paintActive();
    openPanel();
  }

  function suggest(q) {
    if (!SUGGEST_URL) return;
    if (cache[q]) { render(cache[q], q); maybeSmart(q, cache[q]); return; }
    if (controller) { try { controller.abort(); } catch (e) {} }
    controller = ("AbortController" in win) ? new AbortController() : null;
    getJSON(suggestUrl(q, false), controller ? controller.signal : undefined).then(function (data) {
      if (q !== input.value.trim()) return;         // a later keystroke already won
      cache[q] = data;
      if (data && Array.isArray(data.popular) && data.popular.length) popular = data.popular;
      if (data && data.placeholder && !placeholderSettled) {
        placeholderSettled = true;
        endpointPlaceholder = String(data.placeholder);
        input.placeholder = derivedPlaceholder();
      }
      render(data, q);
      maybeSmart(q, data);
    }).catch(function (e) {
      if (e && e.name === "AbortError") return;
      if (q !== input.value.trim()) return;
      render({ ok: false, reason: "failed",
               error: "Could not reach this app's server — " + ((e && e.message) || "network error") + "." }, q);
    });
  }

  function idleOrClose() { if (SUGGEST_URL) drawIdle(); else closePanel(); }

  function onType() {
    var q = input.value.trim();
    clear.hidden = !q;
    if (q === lastQuery) return;
    lastQuery = q;
    cancelSmart();
    if (timer) clearTimeout(timer);
    if (!q) { idleOrClose(); return; }
    if (q.length < MIN_CHARS) { closePanel(); return; }
    timer = setTimeout(function () { suggest(q); }, 140);   // one request per burst of typing
  }

  /* ------------------------------------------------------------------- wiring */

  clear.hidden = !input.value.trim();
  input.addEventListener("input", onType);
  input.addEventListener("focus", function () { if (!input.value.trim()) idleOrClose(); });
  clear.addEventListener("click", function () {
    input.value = "";
    lastQuery = null;
    clear.hidden = true;
    idleOrClose();
    try { input.focus(); } catch (e) {}
  });
  input.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { closePanel(); return; }
    if (!isOpen() || !options.length) return;
    if (e.key === "ArrowDown") {
      e.preventDefault(); active = (active + 1) % options.length; paintActive();
    } else if (e.key === "ArrowUp") {
      e.preventDefault(); active = (active - 1 + options.length) % options.length; paintActive();
    } else if (e.key === "Enter" && active >= 0) {
      // Enter on a highlighted row OPENS it (a choice, reported); Enter with nothing highlighted
      // runs the search (not a choice, reports nothing).
      e.preventDefault();
      var entry = options[active];
      choose(entry);
      win.location.href = entry.node.href;
    }
  });
  form.addEventListener("submit", function () { recentPush(input.value); });
  doc.addEventListener("click", function (e) { if (!form.contains(e.target)) closePanel(); });

  // ONE keyboard way in: Ctrl/Cmd-K always; the bare hotkey only while the person is not typing
  // into something else, where it would land in their text.
  doc.addEventListener("keydown", function (e) {
    var focused = doc.activeElement;
    var typing = focused && (focused.tagName === "INPUT" || focused.tagName === "TEXTAREA" ||
                             focused.tagName === "SELECT" || focused.isContentEditable);
    if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      try { input.focus(); input.select(); } catch (err) {}
      return;
    }
    if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
    var key = hotkey();
    if (key && e.key === key) {
      e.preventDefault();
      try { input.focus(); input.select(); } catch (err) {}
    }
  });

  win.addEventListener("hub:component-props", function (e) {
    var det = (e && e.detail) || {};
    if (det.app && det.app !== D.app) return;
    takeProps(det.props || det);
    input.placeholder = derivedPlaceholder();
  });
  if (D.props) {
    fetch(D.props + (D.props.indexOf("?") === -1 ? "?" : "&") + "component=search-bar",
          { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (b) {
        if (!b || b.ok === false || !b.props) return;
        takeProps(b.props);
        input.placeholder = derivedPlaceholder();
      }).catch(function () {});
  }

  mount.setAttribute("data-sb-version", VERSION);
})();
