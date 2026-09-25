/* App banner -- ONE shared header every adopting app LINKS from the hub (never copies).

   An app declares what it is and what it has; the banner draws the chrome, and the person's
   presentation preferences follow them from app to app. Mount contract (patterns/app-banner.md
   is the full reference):

     <link rel="stylesheet" href="/hub/components/banner/banner.css">
     <header id="app-banner" data-app="budget-app" data-app-name="Budget"
             data-actor="Alice Example" data-role="Admin" ...>
       <script type="application/json" data-ab="nav">[{"label":"Overview","href":"/"}]</script>
       <div data-ab-slot> the app's own controls </div>
     </header>
     <script src="/hub/components/banner/banner.js" defer></script>

   What it gives every adopter, and where each part lives below:
     PREFERENCES   theme, size, text, face, motion, help switches -- saved to the person's hub
                   record, everywhere or for this app only, asked AFTER the change.
     NAVIGATION    pages declared once; a strip under the band or a sidebar with a rail.
     YOUR APPS     the apps this person can reach, with stars.
     THE DRAWER    one sidebar from the avatar: who you are, your apps, the app's own items,
                   an Admins-only section, How this works, Sign out.
     HELP          info bubbles on anything marked data-ab-info; a guided tour and an "About
                   this app" read-me derived from what is on screen when opened.
     PEOPLE        a People and Permissions box speaking the data-access-url contract.
     COMPONENTS    this app's settings for the hosted components, handed on to them as
                   window.HubComponentProps + a `hub:component-props` event, and a Component
                   settings box (drawn from the hub's schema) for whoever the hub says may edit.
   Standard browser APIs only; no framework, no build step. */
(function () {
  "use strict";
  var doc = document, win = window;
  var mount = doc.getElementById("app-banner");
  if (!mount || mount.getAttribute("data-ab-ready")) return;
  var D = mount.dataset;
  var APP = String(D.app || "app").trim().toLowerCase();
  var APP_NAME = D.appName || APP;
  var HUB = String(D.hub || "/hub").replace(/\/+$/, "");
  var PROFILE_URL = D.profileUrl === undefined ? HUB + "/api/profile" : D.profileUrl;
  var LOG = "[app-banner] ";

  /* ------------------------------------------------------------- helpers ---- */
  function el(tag, attrs, kids) {
    var n = doc.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "text") n.textContent = v;
      else if (k === "class") n.className = v;
      else if (v === true) n.setAttribute(k, "");
      else n.setAttribute(k, v);
    });
    (kids || []).forEach(function (c) {
      if (c === null || c === undefined || c === false) return;
      n.appendChild(typeof c === "string" ? doc.createTextNode(c) : c);
    });
    return n;
  }
  function island(name) {
    var node = mount.querySelector('script[type="application/json"][data-ab="' + name + '"]');
    if (!node) return null;
    try { return JSON.parse(node.textContent || "null"); }
    catch (e) { console.warn(LOG + "the '" + name + "' island is not valid JSON and was ignored."); return null; }
  }
  function list(v) { return Array.isArray(v) ? v : []; }
  function vis(n) { return !!(n && n.getClientRects && n.getClientRects().length); }
  function q(sel) { try { return mount.querySelector(sel) || doc.querySelector(sel); } catch (e) { return null; } }
  /* A version the way its app writes it, with exactly ONE leading "v": an app is entitled to
     write "1.4.0" or "v1.4.0", and prefixing blindly prints "vv1.4.0". */
  function vstr(v) {
    v = String(v == null ? "" : v).trim();
    return !v ? "" : (/^v/i.test(v) ? v : "v" + v);
  }
  /* PLACEMENT UNDER THE PERSON'S ZOOM. With `zoom` on <html> (the Size preference), a
     rectangle from getBoundingClientRect() is in ZOOMED pixels while a fixed element's
     left/top are multiplied by the zoom again -- measured: style left 100px draws at 110 at
     zoom 1.1. Every surface placed against an anchor would land 10% off, further the further
     right it sits. So all placement math runs in the page's own (unzoomed) pixels: rects and
     window size divided by the zoom, offsetWidth already there. */
  function zf() { return parseFloat(doc.documentElement.style.zoom) || 1; }
  function box(node) {
    var r = node.getBoundingClientRect(), z = zf();
    return { left: r.left / z, top: r.top / z, right: r.right / z, bottom: r.bottom / z,
             width: r.width / z, height: r.height / z };
  }
  function vw() { return win.innerWidth / zf(); }
  function vh() { return win.innerHeight / zf(); }
  function initials(name) {
    var parts = String(name || "").trim().split(/[\s._@-]+/).filter(Boolean);
    return ((parts[0] || "?").charAt(0) + (parts.length > 1 ? parts[parts.length - 1].charAt(0) : "")).toUpperCase();
  }

  /* ---------------------------------------------------------- one icon set ----
     Every glyph is drawn from ONE stroke set (24px grid, 2px stroke, round caps -- the Lucide
     family's geometry) so the band never mixes two visual languages. Each <svg> carries its
     size, fill and stroke as PRESENTATION ATTRIBUTES and the stylesheet re-asserts them on an
     enumerated class: a host page's `svg { fill: currentColor }` or `svg { width: 100% }`
     reset would otherwise turn every outline into a blob or a page-wide glyph. */
  var ICON = {
    home: ["M3 10.5 12 3l9 7.5", "M5 9.5V21h14V9.5", "M10 21v-6h4v6"],
    apps: ["M4 4h6v6H4z", "M14 4h6v6h-6z", "M4 14h6v6H4z", "M14 14h6v6h-6z"],
    star: ["M12 3l2.8 5.7 6.2.9-4.5 4.4 1.1 6.2L12 17.3l-5.6 2.9 1.1-6.2L3 9.6l6.2-.9z"],
    user: ["M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z", "M4 21a8 8 0 0 1 16 0"],
    users: ["M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2", "M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
            "M22 21v-2a4 4 0 0 0-3-3.9", "M16 3.1a4 4 0 0 1 0 7.8"],
    sun: ["M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8z", "M12 2v2", "M12 20v2", "M4.9 4.9l1.4 1.4",
          "M17.7 17.7l1.4 1.4", "M2 12h2", "M20 12h2", "M4.9 19.1l1.4-1.4", "M17.7 6.3l1.4-1.4"],
    moon: ["M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"],
    help: ["M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z", "M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3", "M12 17h.01"],
    info: ["M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z", "M12 16v-4", "M12 8h.01"],
    book: ["M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5z", "M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5"],
    tour: ["M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z", "M16.2 7.8l-2.1 6.3-6.3 2.1 2.1-6.3z"],
    logout: ["M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4", "M16 17l5-5-5-5", "M21 12H9"],
    x: ["M18 6 6 18", "M6 6l12 12"],
    down: ["M6 9l6 6 6-6"],
    right: ["M9 6l6 6-6 6"],
    "rail-close": ["M3 5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z", "M9 3v18", "M16 15l-3-3 3-3"],
    "rail-open": ["M3 5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z", "M9 3v18", "M14 9l3 3-3 3"],
    shield: ["M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"],
    sliders: ["M4 21v-7", "M4 10V3", "M12 21v-9", "M12 8V3", "M20 21v-5", "M20 12V3", "M1 14h6", "M9 8h6", "M17 16h6"],
    check: ["M20 6 9 17l-5-5"],
    plus: ["M12 5v14", "M5 12h14"],
    image: ["M3 5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
            "M8.5 10a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3z", "M21 15l-5-5L5 21"],
    eye: ["M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z", "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"],
    external: ["M15 3h6v6", "M10 14 21 3", "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"],
    chart: ["M3 3v18h18", "M7 15l4-4 3 3 5-6"],
    list: ["M8 6h13", "M8 12h13", "M8 18h13", "M3 6h.01", "M3 12h.01", "M3 18h.01"],
    file: ["M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z", "M14 3v6h6"],
    calendar: ["M3 6a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z", "M16 2v4", "M8 2v4", "M3 10h18"],
    settings: ["M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
               "M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"]
  };
  var SVGNS = "http://www.w3.org/2000/svg";
  function icon(name, extra, filled) {
    var s = doc.createElementNS(SVGNS, "svg");
    s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("width", "18"); s.setAttribute("height", "18");
    s.setAttribute("fill", filled ? "currentColor" : "none");
    s.setAttribute("stroke", "currentColor"); s.setAttribute("stroke-width", "2");
    s.setAttribute("stroke-linecap", "round"); s.setAttribute("stroke-linejoin", "round");
    s.setAttribute("aria-hidden", "true"); s.setAttribute("focusable", "false");
    s.setAttribute("class", "ab-i" + (filled ? " ab-i--fill" : "") + (extra ? " " + extra : ""));
    (ICON[name] || ICON.info).forEach(function (d) {
      var p = doc.createElementNS(SVGNS, "path"); p.setAttribute("d", d); s.appendChild(p);
    });
    return s;
  }

  /* ------------------------------------------------------------ CSRF token ----
     Three sources, most reliable first: <body data-csrf> (works even with an HttpOnly cookie),
     a rendered csrfmiddlewaretoken input, then the cookie. The cookie is read BY NAME before by
     suffix, because several apps on one host each rename their cookie ("budget_app_csrftoken")
     and, with no cookie path set, every one of them sits at "/" and is visible on every app's
     page. Keeping "the last cookie ending in csrftoken" sends ANOTHER app's secret and the
     server answers 403 while the page renders perfectly. So: this app's own name, then
     Django's default, and only then any name ending in csrftoken. */
  function csrf() {
    var body = doc.body && doc.body.getAttribute("data-csrf");
    if (body) return body;
    var input = doc.querySelector('input[name="csrfmiddlewaretoken"]');
    if (input && input.value) return input.value;
    var jar = {}, last = "";
    String(doc.cookie || "").split(";").forEach(function (part) {
      var eq = part.indexOf("=");
      if (eq < 0) return;
      var name = part.slice(0, eq).trim();
      if (!/csrftoken$/i.test(name)) return;
      jar[name] = last = part.slice(eq + 1).trim();    // last occurrence wins, as Django parses
    });
    var own = APP.replace(/-/g, "_") + "_csrftoken";
    var pick = jar[own] || jar.csrftoken || last;
    try { return pick ? decodeURIComponent(pick) : ""; } catch (e) { return pick || ""; }
  }

  /* ---------------------------------------------------- the person's prefs ----
     The person's record lives at the hub (hub_core/profiles.py). localStorage is a CACHE for
     the first paint only -- reading size and theme over the network before drawing would flash
     the wrong page on every load -- and is reconciled the moment the record answers. */
  var PREF_KEY = "ab:prefs";
  var APP_SCOPED = ["theme", "motion", "text", "ui", "font", "nav_open", "tips", "help"];
  var PREFS = { kind: "initials", image: "", theme: "", motion: "full", text: 100, ui: 100,
                font: "", nav_open: "", tips: "on", help: "on", starred: [], apps: {} };
  var REACH = { apps: [], granted_without_url: 0, loaded: false };
  var PROFILE_STATE = { lane: "unknown", error: "", saving: false };

  function adopt(o) {
    if (!o || typeof o !== "object") return;
    Object.keys(PREFS).forEach(function (k) { if (o[k] !== undefined && o[k] !== null) PREFS[k] = o[k]; });
  }
  try { adopt(JSON.parse(localStorage.getItem(PREF_KEY) || "null")); } catch (e) { /* private window */ }
  function writeCache() { try { localStorage.setItem(PREF_KEY, JSON.stringify(PREFS)); } catch (e) {} }
  function hereOverride() { return (PREFS.apps && PREFS.apps[APP]) || {}; }
  /* The effective value HERE: this app's override when it has one, else the everywhere-value.
     hub_core/profiles.resolve() is the same rule for a server-side reader; APP_SCOPED on both
     sides is the pair to change together. */
  function eff(key, draft) {
    if (draft && draft[key] !== undefined) return draft[key];
    var here = hereOverride();
    return here[key] !== undefined && here[key] !== null ? here[key] : PREFS[key];
  }
  /* The theme the page is ACTUALLY rendering in: the attribute when one is set, otherwise the
     operating system's preference. Reading only the stored preference made a toggle labelled
     "Dark" do nothing on a page that was already dark by media query. */
  function currentTheme() {
    var t = doc.documentElement.getAttribute("data-theme");
    if (t === "light" || t === "dark") return t;
    return win.matchMedia && win.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  var DRAFT = null;          // an unsaved preference change, previewed on the page
  function applyPrefs() {
    var root = doc.documentElement;
    if (!("themeExternal" in D)) {           // an app that owns its theme attribute says so
      var th = eff("theme", DRAFT);
      if (th === "light" || th === "dark") { root.setAttribute("data-theme", th); root.style.colorScheme = th; }
      else if (mount.getAttribute("data-ab-set-theme") === "1") { root.removeAttribute("data-theme"); root.style.colorScheme = ""; }
      mount.setAttribute("data-ab-set-theme", th ? "1" : "0");
    }
    var ui = Number(eff("ui", DRAFT)) || 100, text = Number(eff("text", DRAFT)) || 100;
    root.style.zoom = ui === 100 ? "" : String(ui / 100);
    /* The zoom factor is PUBLISHED: zoom scales viewport units too, so a fixed surface sized in
       vh comes out taller than the window at Large. banner.css divides by --ab-zoom, and an
       app's own fixed layout can do the same. */
    if (ui === 100) root.style.removeProperty("--ab-zoom"); else root.style.setProperty("--ab-zoom", String(ui / 100));
    root.style.fontSize = text === 100 ? "" : text + "%";
    root.setAttribute("data-ab-motion", eff("motion", DRAFT) === "reduce" ? "reduce" : "full");
    var font = eff("font", DRAFT);
    if (font) root.setAttribute("data-ab-font", font); else root.removeAttribute("data-ab-font");
    root.setAttribute("data-ab-tips", eff("tips", DRAFT) === "off" ? "off" : "on");
    root.setAttribute("data-ab-help", eff("help", DRAFT) === "off" ? "off" : "on");
    if (eff("tips", DRAFT) === "off") hideInfo(true);
    paintAvatar();
    try { doc.dispatchEvent(new CustomEvent("app-banner:prefs", { detail: resolved() })); } catch (e) {}
  }
  function resolved() {
    var out = {};
    Object.keys(PREFS).forEach(function (k) { out[k] = PREFS[k]; });
    APP_SCOPED.forEach(function (k) { out[k] = eff(k, DRAFT); });
    return out;
  }
  /* A profile lane that is NOT THERE is not a failure: 404/501/503 mean this page has no
     route to the person's hub record (no sign-in resolver, or the hub unreachable). The choice
     is kept in this browser and the page is already right, so nobody is told "HTTP 404" over
     their own icon. Anything else (403, 500) IS worth saying. */
  function profileAnswer(r) {
    if (r.status === 404 || r.status === 501 || r.status === 503) { PROFILE_STATE.lane = "absent"; return null; }
    if (!r.ok) throw new Error("the profile route answered HTTP " + r.status);
    PROFILE_STATE.lane = "live";
    return r.json();
  }
  function profileUrl() {
    return PROFILE_URL + (PROFILE_URL.indexOf("?") >= 0 ? "&" : "?") + "app=" + encodeURIComponent(APP);
  }
  function loadProfile() {
    if (!PROFILE_URL) { PROFILE_STATE.lane = "absent"; return; }
    fetch(profileUrl(), { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(profileAnswer)
      .then(function (body) {
        var d = body && body.data;
        if (!d) return;
        adopt(d.prefs);
        REACH = { apps: list(d.apps), granted_without_url: d.granted_without_url || 0, loaded: true };
        writeCache(); applyPrefs(); redrawOpen();
        // The first paint used this browser's cache (or the default); the record may say the
        // sidebar is a rail here. Redraw only when it actually differs.
        if (NAV_STYLE === "sidebar" && navNode && navNode.classList.contains("ab-sb-rail") === sidebarOpen()) buildNav();
      })
      .catch(function (err) { PROFILE_STATE.error = String(err && err.message || err); redrawOpen(); });
  }
  /* SAVE one change. `payload` is exactly what changed ({theme:"dark"} everywhere, or
     {apps:{<app>:{theme:"dark"}}} for this app only); the server merges per key and per app,
     so saving THIS app's override never drops another app's. A failed save is SAID. */
  function savePrefs(payload, done) {
    writeCache(); applyPrefs();
    if (!PROFILE_URL || PROFILE_STATE.lane === "absent") { if (done) done(true); return; }
    PROFILE_STATE.saving = true; PROFILE_STATE.error = "";
    fetch(profileUrl(), { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": csrf() },
      body: JSON.stringify({ prefs: payload }) })
      .then(profileAnswer)
      .then(function (body) {
        PROFILE_STATE.saving = false;
        if (body && body.data) {
          adopt(body.data.prefs);
          if ((body.data.ignored || []).length) console.warn(LOG + "the hub ignored: " + body.data.ignored.join(", "));
        }
        writeCache(); applyPrefs(); if (done) done(true);
      })
      .catch(function (err) {
        PROFILE_STATE.saving = false; PROFILE_STATE.error = String(err && err.message || err);
        if (done) done(false);
      });
  }
  /* Set one key the way the person expects: where THIS app already overrides it, change the
     override (or the change would visibly do nothing); otherwise change it everywhere. */
  function setHere(key, value, done) {
    if (hereOverride()[key] !== undefined) {
      PREFS.apps = PREFS.apps || {}; PREFS.apps[APP] = PREFS.apps[APP] || {};
      PREFS.apps[APP][key] = value;
      var o = {}; o[APP] = {}; o[APP][key] = value;
      savePrefs({ apps: o }, done);
    } else {
      PREFS[key] = value;
      var p = {}; p[key] = value;
      savePrefs(p, done);
    }
  }

  /* -------------------------------------------------------------- overlays ----
     ONE open surface at a time. Every surface registers its closer here; Escape, a click
     outside, or opening another closes it and hands focus back to what opened it. */
  var openThing = null;
  var scrim = el("div", { class: "ab-scrim ab-surface", hidden: true });
  function closeOpen() {
    if (!openThing) return;
    var t = openThing; openThing = null;
    try { t.close(); } catch (e) { console.warn(LOG + "a surface failed to close: " + e); }
    scrim.hidden = true;
    if (t.opener && t.opener.isConnected) { try { t.opener.focus({ preventScroll: true }); } catch (e) {} }
  }
  function openAs(kind, opener, close, useScrim) {
    closeOpen();
    openThing = { kind: kind, opener: opener || null, close: close };
    scrim.hidden = !useScrim;
  }
  function redrawOpen() {
    if (!openThing) return;
    if (openThing.kind === "drawer") fillDrawer();
    else if (openThing.kind === "apps") fillApps();
  }
  scrim.addEventListener("click", closeOpen);
  doc.addEventListener("keydown", function (e) { if (e.key === "Escape" && openThing) { closeOpen(); } });
  doc.addEventListener("click", function (e) {
    if (!openThing) return;
    /* A DETACHED target is not an outside click: a control that redraws its own panel inside
       its click handler has left the document by the time this runs, and closest() on it can
       never reach the panel -- which would close the panel the instant anything in it is
       pressed. isConnected is the exact question. */
    if (!e.target.isConnected) return;
    if (e.target.closest(".ab-surface, .ab-btn, .ab-avatar, .ab-tabgroup")) return;
    closeOpen();
  });

  /* ------------------------------------------------------ chips and bubbles ----
     TWO different things. A CHIP names a glyph-only control instantly (data-ab-tip). A BUBBLE
     explains what something is FOR, after a short pause, for anything on the page carrying
     data-ab-info="..." (and optional data-ab-info-title) -- the band's own tabs and
     preferences, and whatever key things the app marks. Bubbles are the PERSON's choice
     (tips on/off), and every bubble carries the way out. */
  var chip = el("div", { class: "ab-chip ab-surface", role: "tooltip", hidden: true });
  function showChip(node) {
    var text = node.getAttribute("data-ab-tip");
    if (!text || (node.hasAttribute("data-ab-info") && infoOn())) return;
    chip.textContent = text; chip.hidden = false;
    var r = box(node);
    var left = Math.max(6, Math.min(r.left + r.width / 2 - chip.offsetWidth / 2, vw() - chip.offsetWidth - 6));
    var top = r.bottom + 6 + chip.offsetHeight > vh() ? r.top - chip.offsetHeight - 6 : r.bottom + 6;
    chip.style.left = Math.round(left) + "px"; chip.style.top = Math.round(top) + "px";
  }
  function hideChip() { chip.hidden = true; }
  doc.addEventListener("pointerover", function (e) {
    var n = e.target.closest && e.target.closest("[data-ab-tip]");
    if (n && e.pointerType !== "touch") showChip(n); else hideChip();
  });
  doc.addEventListener("focusin", function (e) {
    var n = e.target.closest && e.target.closest("[data-ab-tip]");
    if (n && n.matches(":focus-visible")) showChip(n); else hideChip();
  });
  doc.addEventListener("pointerdown", hideChip, true);

  var INFO_DELAY = 320, INFO_GRACE = 180;
  var info = el("div", { class: "ab-info ab-surface", role: "tooltip", hidden: true });
  var infoTitle = el("strong", { class: "ab-info-title" });
  var infoText = el("p", { class: "ab-info-text" });
  var infoHideAll = el("button", { class: "ab-info-hide", type: "button", text: "Hide all info bubbles" });
  info.appendChild(infoTitle); info.appendChild(infoText);
  info.appendChild(el("div", { class: "ab-info-foot" }, [infoHideAll]));
  var infoFor = null, infoShowT = 0, infoHideT = 0, infoPointerIn = false;
  function infoOn() { return doc.documentElement.getAttribute("data-ab-tips") !== "off"; }
  function infoName(node) {
    return node.getAttribute("data-ab-info-title") || node.getAttribute("aria-label") ||
           (node.innerText || node.textContent || "").trim().split("\n")[0] || "";
  }
  /* Placed against the anchor and the window: below, above, right, left -- the first that
     fits. A sidebar row asks for the side (below would cover the next row) and a drawer row
     for the left (over the page, not over the buttons it explains). Clamped to the window. */
  function placeInfo(node) {
    var r = box(node), m = 8, gap = 10;
    info.style.left = "0px"; info.style.top = "0px";
    var w = info.offsetWidth, h = info.offsetHeight, W = vw(), H = vh();
    var side = node.closest(".ab-sb") ? "right" : node.closest(".ab-drawer") ? "left" : "below";
    var fits = { below: r.bottom + gap + h <= H - m, above: r.top - gap - h >= m,
                 right: r.right + gap + w <= W - m, left: r.left - gap - w >= m };
    var order = side === "right" ? ["right", "left", "below", "above"]
              : side === "left" ? ["left", "right", "below", "above"] : ["below", "above", "right", "left"];
    var pick = order.filter(function (k) { return fits[k]; })[0] || order[0];
    var left, top;
    if (pick === "below" || pick === "above") {
      left = r.left + r.width / 2 - w / 2;
      top = pick === "below" ? r.bottom + gap : r.top - gap - h;
    } else {
      top = r.top + r.height / 2 - h / 2;
      left = pick === "right" ? r.right + gap : r.left - gap - w;
    }
    left = Math.max(m, Math.min(left, W - w - m)); top = Math.max(m, Math.min(top, H - h - m));
    info.setAttribute("data-place", pick);
    info.style.left = Math.round(left) + "px"; info.style.top = Math.round(top) + "px";
  }
  function showInfo(node) {
    if (!infoOn()) return;
    var text = node.getAttribute("data-ab-info");
    if (!text || node.getAttribute("aria-expanded") === "true") return;
    // On the rail a fold's own flyout is the explanation; a bubble beside it would cover it.
    if (node.classList.contains("ab-sb-head") && node.closest(".ab-sb-rail")) return;
    hideChip();
    infoFor = node;
    infoTitle.textContent = infoName(node); infoTitle.hidden = !infoTitle.textContent;
    infoText.textContent = text;
    info.hidden = false; placeInfo(node);
  }
  /* The grace timer HIDES and touches nothing else: moving straight from one explained thing to
     the next fires the old one's hide and then the new one's show, and a hide that also
     cancelled the pending show meant only the FIRST hover ever bubbled. Only a decision (a
     click, Escape, a scroll, the switch going off) cancels a pending show. */
  function hideInfo(now) {
    clearTimeout(infoHideT); infoHideT = 0;
    if (now) { clearTimeout(infoShowT); infoShowT = 0; } else if (infoPointerIn) return;
    infoFor = null; info.hidden = true;
  }
  function infoArm(node) {
    if (node === infoFor) { clearTimeout(infoHideT); infoHideT = 0; return; }
    clearTimeout(infoShowT);
    infoShowT = setTimeout(function () { infoShowT = 0; showInfo(node); }, INFO_DELAY);
  }
  function infoDisarm() {
    clearTimeout(infoShowT); infoShowT = 0; clearTimeout(infoHideT);
    infoHideT = setTimeout(function () { if (!infoPointerIn) hideInfo(false); }, INFO_GRACE);
  }
  doc.addEventListener("pointerover", function (e) {
    if (e.pointerType === "touch") return;               // a tap is a press, never a hover
    if (info.contains(e.target)) { infoPointerIn = true; clearTimeout(infoHideT); return; }
    var node = e.target.closest && e.target.closest("[data-ab-info]");
    if (node) infoArm(node); else if (infoFor || infoShowT) infoDisarm();
  });
  doc.addEventListener("pointerout", function (e) {
    var to = e.relatedTarget;
    if (info.contains(e.target)) { if (!to || !info.contains(to)) { infoPointerIn = false; infoDisarm(); } return; }
    var node = e.target.closest && e.target.closest("[data-ab-info]");
    if (node && (!to || !node.contains(to))) infoDisarm();
  });
  doc.addEventListener("focusin", function (e) {
    var node = e.target.closest && e.target.closest("[data-ab-info]");
    if (node && node.matches(":focus-visible")) showInfo(node);
  });
  doc.addEventListener("focusout", function (e) { if (e.target.closest && e.target.closest("[data-ab-info]")) infoDisarm(); });
  doc.addEventListener("click", function (e) { if (!info.contains(e.target)) hideInfo(true); }, true);
  doc.addEventListener("keydown", function (e) { if (e.key === "Escape") hideInfo(true); });
  doc.addEventListener("scroll", function (e) { if (!info.contains(e.target)) hideInfo(true); }, { capture: true, passive: true });
  win.addEventListener("resize", function () { hideInfo(true); });

  /* THE WAY OUT, from any bubble: off everywhere, at once, with Undo -- and this app's own
     override of the switch is cleared too, or a per-app "on" set weeks ago would beat the
     everywhere-"off" and the button would visibly do nothing. Undo puts back exactly what was
     there. */
  var toast = null;
  function dropToast() { if (toast && toast.parentNode) toast.parentNode.removeChild(toast); toast = null; }
  function sayToast(text, undo) {
    dropToast();
    toast = el("div", { class: "ab-toast ab-surface", role: "status" }, [el("span", { text: text })]);
    if (undo) {
      var b = el("button", { class: "ab-toast-undo", type: "button", text: "Undo" });
      b.addEventListener("click", function (ev) { ev.stopPropagation(); undo(); dropToast(); });
      toast.appendChild(b);
    }
    doc.body.appendChild(toast);
    setTimeout(dropToast, 9000);
  }
  infoHideAll.addEventListener("click", function (e) {
    e.stopPropagation();
    infoPointerIn = false; hideInfo(true);
    var before = { tips: PREFS.tips, here: hereOverride().tips };
    PREFS.tips = "off";
    var payload = { tips: "off" };
    if (before.here !== undefined) {
      delete PREFS.apps[APP].tips;
      payload.apps = {}; payload.apps[APP] = { tips: null };
    }
    savePrefs(payload);
    sayToast("Info bubbles are off in every app. Turn them back on under your icon → Preferences.",
      function () {
        PREFS.tips = before.tips || "on";
        var back = { tips: PREFS.tips };
        if (before.here !== undefined) {
          PREFS.apps[APP] = PREFS.apps[APP] || {}; PREFS.apps[APP].tips = before.here;
          back.apps = {}; back.apps[APP] = { tips: before.here };
        }
        savePrefs(back); redrawOpen();
      });
    redrawOpen();
  });

  /* ---------------------------------------------------------------- the band ---- */
  var slot = mount.querySelector("[data-ab-slot]");
  var NAV = list(island("nav"));
  var band = el("div", { class: "ab-band" });
  var home = el("a", { class: "ab-home", href: D.home || "./", "data-ab-tip": APP_NAME + " home" });
  var markBox = el("span", { class: "ab-mark", "aria-hidden": "true" });
  if (D.mark) markBox.appendChild(el("img", { src: D.mark, alt: "" }));
  else markBox.textContent = initials(APP_NAME);
  home.appendChild(markBox);
  home.appendChild(el("span", { class: "ab-name" }, [
    el("span", { class: "ab-app", text: APP_NAME }),
    D.appSub ? el("span", { class: "ab-sub", text: D.appSub }) : null]));
  var slotBox = el("div", { class: "ab-slot" });
  if (slot) slotBox.appendChild(slot);
  var controls = el("div", { class: "ab-controls" });
  function bandBtn(name, label, extra) {
    return el("button", { class: "ab-btn" + (extra ? " " + extra : ""), type: "button",
                          "aria-label": label, "data-ab-tip": label }, [icon(name)]);
  }
  var tourBtn = bandBtn("tour", "Guided tour", "ab-help-btn");
  tourBtn.setAttribute("data-ab-help", "");
  var appsBtn = bandBtn("apps", "Your apps");
  appsBtn.setAttribute("aria-haspopup", "dialog"); appsBtn.setAttribute("aria-expanded", "false");
  var avatar = el("button", { class: "ab-avatar", type: "button", "aria-haspopup": "dialog",
                              "aria-expanded": "false",
                              "aria-label": "Your settings" + (D.actor ? " — " + D.actor : ""),
                              "data-ab-tip": D.actor || "Your settings" });
  controls.appendChild(tourBtn);
  controls.appendChild(appsBtn);
  controls.appendChild(avatar);
  band.appendChild(home);
  band.appendChild(slotBox);
  /* ONE hairline between the app's own controls and the band's, whatever the slot holds. It
     used to be a border on the slot, so an empty slot drew a hairline around nothing and a
     full one drew it in the wrong place. */
  band.appendChild(el("span", { class: "ab-hair", "aria-hidden": "true" }));
  band.appendChild(controls);
  // The app's own markup inside the mount stays in the document, hidden -- its script may
  // still bind by id -- except the islands, which are read above.
  Array.prototype.forEach.call(mount.children, function (c) {
    if (c.tagName !== "SCRIPT") c.setAttribute("data-ab-was-content", "");
  });
  mount.classList.add("ab-root");
  mount.insertBefore(band, mount.firstChild);

  function paintAvatar() {
    avatar.textContent = "";
    var src = D.avatar || (PREFS.kind === "image" && PREFS.image ? PREFS.image : "");
    if (src) avatar.appendChild(el("img", { src: src, alt: "" }));
    else avatar.appendChild(el("span", { text: initials(D.actor || D.actorSub || "you") }));
  }

  /* ----------------------------------------------------------- the app slot ----
     The slot SHOWS only if something in it renders. Measured, not guessed from tags -- a
     button its own stylesheet hides is still a button -- and measured with the slot's OWN
     "empty" verdict lifted: that verdict is display:none on the box these children sit in, so
     once it said "empty" every later reading was 0 by construction and the slot could never
     come back. An app that cloaks its shell until its script boots (x-cloak, a hydration
     gate) hit exactly that on its first paint. Re-measured whenever the slot's content
     changes, because a Stop button that exists only mid-operation comes and goes. */
  function slotSync() {
    if (!slot) { slotBox.classList.add("ab-slot--empty"); return; }
    slotBox.classList.remove("ab-slot--empty");
    var shows = Array.prototype.some.call(slot.querySelectorAll("*"), function (n) {
      if (/^(SCRIPT|LINK|STYLE|TEMPLATE|META|NOSCRIPT)$/.test(n.tagName)) return false;
      var r = n.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    }) || !!(slot.textContent || "").trim() && slot.getBoundingClientRect().width > 0;
    slotBox.classList.toggle("ab-slot--empty", !shows);
  }
  if (slot && win.MutationObserver) {
    new MutationObserver(function () { slotSync(); })
      .observe(slot, { childList: true, subtree: true, attributes: true, attributeFilter: ["hidden", "class", "style"] });
  }

  /* -------------------------------------------------------------- navigation ----
     Pages are declared ONCE (the `nav` island): [{label, href, icon, hint, sep, current,
     count, countId, children:[...]}]. data-nav-style picks the rendering -- "strip" (under the
     band; the default) or "sidebar" (down the left, collapsible to a rail). A child list
     becomes a dropdown on the strip and a fold in the sidebar. `hint` becomes the page's info
     bubble; an item without one is reported once, to the adopter, in the console. */
  var NAV_STYLE = D.navStyle === "sidebar" ? "sidebar" : "strip";
  (function reportBareHints() {
    var bare = [];
    (function walk(items) {
      items.forEach(function (n) { if (!n.hint) bare.push(n.label || "(unnamed)"); walk(list(n.children)); });
    })(NAV);
    if (bare.length) console.warn(LOG + bare.length + " nav item(s) carry no `hint`, so hovering them explains nothing: " + bare.join(", ") + ".");
  })();
  function navCount(n, cls) {
    if (n.count === undefined && !n.countId) return null;
    var v = n.count === undefined || n.count === null ? "" : String(n.count);
    // Zero is HIDDEN, not drawn: a grey 0 reads as a real quantity. The app's own script keeps
    // writing the pill by id (countId) exactly as it did before the band existed.
    return el("span", { class: cls, id: n.countId || null, hidden: v === "" || v === "0", text: v });
  }
  function navLink(n, cls) {
    return el("a", { class: cls, href: n.href || "#", "aria-current": n.current ? "page" : null,
                     "data-ab-info": n.hint || null, "data-ab-info-title": n.hint ? n.label : null }, [
      n.icon && ICON[n.icon] ? icon(n.icon, "ab-nav-glyph") : null,
      el("span", { text: n.label || "" }), navCount(n, "ab-count")]);
  }
  /* A menu hanging below the strip must not live inside the strip's CLIP: the strip scrolls
     sideways (overflow-x:auto), which makes the other axis clip too, and a menu at top:100%
     was drawn and invisible. While open it is positioned against the VIEWPORT from its
     button's rectangle, and closes on scroll or resize rather than float detached. */
  function floatPop(pop, anchor) {
    var r = box(anchor);
    pop.style.position = "fixed";
    pop.style.top = Math.round(r.bottom + 4) + "px";
    pop.style.left = Math.round(Math.max(6, Math.min(r.left, vw() - pop.offsetWidth - 6))) + "px";
    var drop = function (e) {
      if (e && e.type === "scroll" && pop.contains(e.target)) return;
      win.removeEventListener("scroll", drop, true); win.removeEventListener("resize", drop);
      if (!pop.hidden) closeOpen();
    };
    requestAnimationFrame(function () { win.addEventListener("scroll", drop, true); win.addEventListener("resize", drop); });
  }
  /* A dropdown that opens on HOVER with intent (the pointer rests HOVER_OPEN ms; leaving
     closes after HOVER_CLOSE ms so crossing into the menu never shuts it), and a CLICK PINS it:
     a person who clicks to open must not get a menu that vanishes under the pointer. A
     hover-opened menu that is clicked stays; a pinned menu clicked again closes. Touch and
     keyboard keep click, Enter/Space and Escape. */
  var HOVER_OPEN = 140, HOVER_CLOSE = 260;
  function navGroup(n) {
    var wrap = el("div", { class: "ab-tabgroup" });
    var btn = el("button", { class: "ab-tab ab-tab-more" + (n.current ? " ab-on" : ""), type: "button",
                             "aria-haspopup": "menu", "aria-expanded": "false" }, [
      n.icon && ICON[n.icon] ? icon(n.icon, "ab-nav-glyph") : null,
      el("span", { text: n.label || "" }), icon("down", "ab-caret")]);
    var pop = el("div", { class: "ab-tabpop ab-surface", role: "menu", hidden: true, "aria-label": n.label || "More" });
    list(n.children).forEach(function (c) {
      pop.appendChild(el("a", { class: c.current ? "ab-on" : null, role: "menuitem", href: c.href || "#",
                                "aria-current": c.current ? "page" : null }, [
        c.icon && ICON[c.icon] ? icon(c.icon, "ab-nav-glyph") : null,
        el("span", null, [el("strong", { text: c.label || "" }), c.hint ? el("small", { text: c.hint }) : null]),
        navCount(c, "ab-count")]));
    });
    var open = false, pinned = false, tOpen = 0, tClose = 0;
    function show(byClick) {
      clearTimeout(tOpen); clearTimeout(tClose);
      if (open) { if (byClick) pinned = true; return; }
      openAs("tabpop", btn, function () { pop.hidden = true; open = false; pinned = false; btn.setAttribute("aria-expanded", "false"); }, false);
      pop.hidden = false; floatPop(pop, btn);
      open = true; pinned = !!byClick;
      btn.setAttribute("aria-expanded", "true");
    }
    btn.addEventListener("click", function (e) {
      e.stopPropagation(); clearTimeout(tOpen);
      if (open && pinned) { closeOpen(); return; }
      show(true);
    });
    if (win.matchMedia && win.matchMedia("(hover: hover) and (pointer: fine)").matches) {
      var enter = function () { clearTimeout(tClose); if (!open) { clearTimeout(tOpen); tOpen = setTimeout(function () { show(false); }, HOVER_OPEN); } };
      var leave = function () {
        clearTimeout(tOpen);
        if (open && !pinned) tClose = setTimeout(function () { if (open && !pinned) closeOpen(); }, HOVER_CLOSE);
      };
      wrap.addEventListener("mouseenter", enter); wrap.addEventListener("mouseleave", leave);
      pop.addEventListener("mouseenter", enter); pop.addEventListener("mouseleave", leave);
    }
    wrap.appendChild(btn); doc.body.appendChild(pop);
    return wrap;
  }
  /* THE SIDEBAR. Open or shut to a rail, per person AND per app (nav_open rides this app's
     override, so a twenty-page app and a four-page app can differ, and it follows the person
     to another machine). On the rail a fold is forced OPEN so its flyout has children to draw
     (a closed <details> renders none), and it opens beside the rail on hover or focus -- the
     rail is never a dead end. Visibility, never display:none, so Tab still reaches the rows.
     Ctrl+B toggles, and no-ops while a field has focus. */
  function sidebarOpen() {
    var v = eff("nav_open");
    return v !== "off";
  }
  function setSidebarOpen(open) {
    PREFS.apps = PREFS.apps || {}; PREFS.apps[APP] = PREFS.apps[APP] || {};
    PREFS.apps[APP].nav_open = open ? "on" : "off";
    var o = {}; o[APP] = { nav_open: open ? "on" : "off" };
    savePrefs({ apps: o });
    buildNav();
  }
  function sidebarRow(n, child) {
    return el("a", { class: "ab-sb-link" + (child ? " ab-sb-child" : "") + (n.current ? " ab-on" : ""),
                     href: n.href || "#", "aria-current": n.current ? "page" : null,
                     "data-ab-tip": n.label || "", "data-ab-info": n.hint || null,
                     "data-ab-info-title": n.hint ? n.label : null }, [
      n.icon && ICON[n.icon] ? icon(n.icon, "ab-sb-glyph") : el("span", { class: "ab-sb-dot" }),
      el("span", { class: "ab-sb-label", text: n.label || "" }), navCount(n, "ab-count")]);
  }
  var navNode = null;
  function buildNav() {
    if (navNode && navNode.parentNode) navNode.parentNode.removeChild(navNode);
    navNode = null;
    doc.documentElement.style.removeProperty("--ab-sidebar-w");
    doc.body.classList.remove("ab-has-sidebar");
    if (!NAV.length) return;
    if (NAV_STYLE === "sidebar") {
      var open = sidebarOpen();
      var aside = el("aside", { class: "ab-sb ab-surface" + (open ? "" : " ab-sb-rail"), "aria-label": "Sections" });
      var toggle = el("button", { class: "ab-sb-toggle", type: "button", "aria-expanded": open ? "true" : "false",
                                  "data-ab-tip": (open ? "Collapse" : "Expand") + " (Ctrl+B)" }, [
        icon("rail-close", "ab-sb-tclose"), icon("rail-open", "ab-sb-topen"),
        el("span", { class: "ab-sb-label", text: "Collapse" })]);
      toggle.addEventListener("click", function (e) { e.stopPropagation(); setSidebarOpen(!sidebarOpen()); });
      aside.appendChild(el("div", { class: "ab-sb-top" }, [toggle]));
      var navList = el("nav", { class: "ab-sb-nav", "aria-label": "Pages" });
      NAV.forEach(function (n) {
        if (n.sep) navList.appendChild(el("span", { class: "ab-sb-sep" }));
        if (!list(n.children).length) { navList.appendChild(sidebarRow(n, false)); return; }
        var fold = el("details", { class: "ab-sb-fold" });
        fold.open = !open || !!n.open || !!n.current || list(n.children).some(function (c) { return c.current; });
        fold.appendChild(el("summary", { class: "ab-sb-link ab-sb-head" + (n.current ? " ab-within" : ""),
                                         "data-ab-tip": n.label || "", "data-ab-info": n.hint || null,
                                         "data-ab-info-title": n.hint ? n.label : null }, [
          n.icon && ICON[n.icon] ? icon(n.icon, "ab-sb-glyph") : el("span", { class: "ab-sb-dot" }),
          el("span", { class: "ab-sb-label", text: n.label || "" }), icon("right", "ab-sb-chev")]));
        var kids = el("div", { class: "ab-sb-kids" });
        list(n.children).forEach(function (c) { kids.appendChild(sidebarRow(c, true)); });
        fold.appendChild(kids);
        navList.appendChild(fold);
      });
      aside.appendChild(navList);
      doc.body.appendChild(aside);
      navNode = aside;
      doc.documentElement.style.setProperty("--ab-sidebar-w", open ? "232px" : "56px");
      doc.body.classList.add("ab-has-sidebar");
      publishChrome();
      return;
    }
    var strip = el("nav", { class: "ab-strip", "aria-label": "Pages" });
    var inner = el("div", { class: "ab-strip-in" });
    NAV.forEach(function (n) {
      if (n.sep) inner.appendChild(el("span", { class: "ab-tab-sep", "aria-hidden": "true" }));
      inner.appendChild(list(n.children).length ? navGroup(n) : navLink(n, "ab-tab" + (n.current ? " ab-on" : "")));
    });
    strip.appendChild(inner);
    mount.appendChild(strip);
    navNode = strip;
    publishChrome();
  }
  doc.addEventListener("keydown", function (e) {
    if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey || String(e.key).toLowerCase() !== "b") return;
    var t = doc.activeElement;
    if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
    if (NAV_STYLE !== "sidebar" || !NAV.length) return;
    e.preventDefault(); setSidebarOpen(!sidebarOpen());
  });

  /* The band's height, PUBLISHED, so an app's sticky elements and this component's own
     drawer and boxes sit under whatever the app keeps pinned at the top. */
  function publishChrome() {
    var h = Math.round(box(mount).height), b = Math.round(box(band).height);
    if (h > 0) doc.documentElement.style.setProperty("--ab-h", h + "px");
    // The drawer hangs from the BAND, over the page strip, so it reads as the band's own.
    if (b > 0) doc.documentElement.style.setProperty("--ab-band-h", b + "px");
  }
  win.addEventListener("resize", publishChrome);

  /* ------------------------------------------------------------ your apps ----
     The apps this person can reach (the hub joins the adopter's grants with its app
     directory), starred first. Stars live on the person's record, so a star set here is a star
     in every app. An app the directory has no address for is not a row -- a row is a door --
     but the count is said, so the omission is visible. */
  var appsPop = el("div", { class: "ab-apps ab-surface", role: "dialog", "aria-label": "Your apps", hidden: true });
  function starred() { return list(PREFS.starred).slice(); }
  function toggleStar(slug) {
    var s = starred(), i = s.indexOf(slug);
    if (i >= 0) s.splice(i, 1); else s.push(slug);
    PREFS.starred = s.slice(0, 12);
    savePrefs({ starred: PREFS.starred }, redrawOpen);
    redrawOpen();
  }
  function appRow(app, withStar) {
    var on = starred().indexOf(app.slug) >= 0;
    var here = app.slug === APP;
    var row = el("div", { class: "ab-app-row" + (here ? " ab-here" : "") }, [
      el("a", { class: "ab-app-link", href: app.url, "aria-current": here ? "page" : null }, [
        el("span", { class: "ab-app-mark", text: initials(app.name) }),
        el("span", { class: "ab-app-text" }, [el("strong", { text: app.name }),
          el("small", { text: here ? "You are here" : (app.role ? app.role : "") })])])]);
    if (withStar) {
      var star = el("button", { class: "ab-star" + (on ? " ab-on" : ""), type: "button",
                                "aria-pressed": on ? "true" : "false",
                                "aria-label": (on ? "Unstar " : "Star ") + app.name,
                                "data-ab-tip": on ? "Unstar" : "Star" }, [icon("star", null, on)]);
      star.addEventListener("click", function (e) { e.stopPropagation(); toggleStar(app.slug); });
      row.appendChild(star);
    }
    return row;
  }
  function sortedApps() {
    var s = starred();
    return REACH.apps.slice().sort(function (a, b) {
      var sa = s.indexOf(a.slug), sb = s.indexOf(b.slug);
      if (sa >= 0 || sb >= 0) return (sa < 0 ? 99 : sa) - (sb < 0 ? 99 : sb);
      return a.name.toLowerCase() < b.name.toLowerCase() ? -1 : 1;
    });
  }
  function fillApps() {
    appsPop.textContent = "";
    appsPop.appendChild(el("div", { class: "ab-pop-head" }, [el("strong", { text: "Your apps" }),
      el("small", { text: REACH.loaded ? REACH.apps.length + " you can open" : "" })]));
    if (!REACH.loaded) {
      appsPop.appendChild(el("p", { class: "ab-empty", text: PROFILE_STATE.lane === "absent"
        ? "This page has no route to your hub record, so the apps you can reach are not known here."
        : (PROFILE_STATE.error ? "Could not read your apps: " + PROFILE_STATE.error : "Reading the apps you can reach…") }));
      return;
    }
    if (!REACH.apps.length) appsPop.appendChild(el("p", { class: "ab-empty", text: "No app has granted you access yet." }));
    var box = el("div", { class: "ab-app-list" });
    sortedApps().forEach(function (a) { box.appendChild(appRow(a, true)); });
    appsPop.appendChild(box);
    if (REACH.granted_without_url) {
      appsPop.appendChild(el("p", { class: "ab-note", text: REACH.granted_without_url +
        " more app" + (REACH.granted_without_url === 1 ? " has" : "s have") +
        " granted you access but has no address in the app directory yet, so " +
        (REACH.granted_without_url === 1 ? "it is" : "they are") + " not listed." }));
    }
  }
  appsBtn.addEventListener("click", function (e) {
    e.stopPropagation();
    if (openThing && openThing.kind === "apps") { closeOpen(); return; }
    openAs("apps", appsBtn, function () { appsPop.hidden = true; appsBtn.setAttribute("aria-expanded", "false"); }, false);
    fillApps(); appsPop.hidden = false; appsBtn.setAttribute("aria-expanded", "true");
    var r = box(appsBtn);
    appsPop.style.top = Math.round(r.bottom + 6) + "px";
    appsPop.style.right = Math.round(Math.max(6, vw() - r.right)) + "px";
  });

  /* ---------------------------------------------------------- the drawer ----
     ONE sidebar from the avatar, under the band (it covers the strip, not the band), in the
     order a person needs it: who you are; your starred apps; what the app declared for the
     person (`profile` island); an ADMINS-ONLY section (the `admin` island and People and
     Permissions); and a pinned foot -- How this works (tour, read-me, the app's `help`
     island), the super-admin card, Sign out. Four colour tiers keep the grounds honest about
     whose a row is: the app's, the hub's, admin, help.
     Roles come from data-role; "admin" anywhere in it is an admin, "super admin" a super
     admin. The band only SHOWS by role -- every rule is the server's to enforce. */
  var drawer = el("aside", { class: "ab-drawer ab-surface", role: "dialog", "aria-label": "Your settings", hidden: true });
  var VIEW = "main";          // main | prefs
  var PERSPECTIVE = "";       // a super admin previewing the drawer as a lower role
  function realRole() { return String(D.role || ""); }
  function effRole() { return PERSPECTIVE || realRole(); }
  function isSuper(role) { return /super\s*-?\s*admin/i.test(role); }
  function isAdmin(role) { return /admin/i.test(role); }
  function item(it, tier) {
    var tag = it.href ? "a" : "button";
    var n = el(tag, { class: "ab-row ab-tier-" + (tier || "app"), href: it.href || null,
                      type: it.href ? null : "button", id: it.id || null,
                      "data-ab-info": it.info || it.hint || null,
                      "data-ab-info-title": it.info || it.hint ? it.label : null }, [
      icon(it.icon && ICON[it.icon] ? it.icon : "right", "ab-row-glyph"),
      el("span", { class: "ab-row-text" }, [el("span", { text: it.label || "" }),
        it.sub ? el("small", { text: it.sub }) : null])]);
    if (!it.href && it.id) {
      // An id-bound item is the APP's action: clicking fires an event the app listens for.
      n.addEventListener("click", function () {
        closeOpen();
        try { doc.dispatchEvent(new CustomEvent("app-banner:action", { detail: { id: it.id } })); } catch (e) {}
      });
    }
    return n;
  }
  function section(title, tier, kids) {
    kids = kids.filter(Boolean);
    if (!kids.length) return null;
    return el("section", { class: "ab-sec ab-tier-" + tier }, [title ? el("h3", { class: "ab-sec-h", text: title }) : null].concat(kids));
  }
  function themeToggle() {
    var dark = currentTheme() === "dark";
    var b = el("button", { class: "ab-btn ab-theme", type: "button",
                           "aria-label": dark ? "Switch to light" : "Switch to dark",
                           "data-ab-tip": dark ? "Light" : "Dark" }, [icon(dark ? "sun" : "moon")]);
    b.addEventListener("click", function (e) {
      e.stopPropagation();
      setHere("theme", currentTheme() === "dark" ? "light" : "dark", redrawOpen);
      fillDrawer();
    });
    return b;
  }
  function identityCard() {
    var mark = el("span", { class: "ab-id-mark" });
    var src = D.avatar || (PREFS.kind === "image" && PREFS.image ? PREFS.image : "");
    if (src) mark.appendChild(el("img", { src: src, alt: "" })); else mark.textContent = initials(D.actor || D.actorSub || "you");
    var change = el("button", { class: "ab-link", type: "button", text: "Change icon",
                                "data-ab-info": "Your initials, or a picture you crop here. It follows you to every app." });
    change.addEventListener("click", function (e) { e.stopPropagation(); openMarkMenu(change); });
    var prefs = el("button", { class: "ab-link", type: "button", text: "Preferences",
                               "data-ab-info": "Theme, size, reading face, motion and help. Save them for every app or just this one." });
    prefs.addEventListener("click", function (e) { e.stopPropagation(); VIEW = "prefs"; DRAFT = {}; fillDrawer(); });
    var who = [D.actor || D.actorSub || "Signed in", effRole()].filter(Boolean).join(" — ");
    return el("div", { class: "ab-id ab-tier-hub" }, [mark, el("div", { class: "ab-id-text" }, [
      el("strong", { text: who }), D.actorSub && D.actor ? el("small", { text: D.actorSub }) : null,
      el("div", { class: "ab-id-links" }, [D.avatar ? null : change, prefs])])]);
  }
  function starredRows() {
    var s = starred();
    var rows = REACH.apps.filter(function (a) { return s.indexOf(a.slug) >= 0 && a.slug !== APP; })
      .sort(function (a, b) { return s.indexOf(a.slug) - s.indexOf(b.slug); })
      .map(function (a) { return appRow(a, false); });
    var all = el("button", { class: "ab-row ab-tier-hub", type: "button" }, [icon("apps", "ab-row-glyph"),
      el("span", { class: "ab-row-text" }, [el("span", { text: REACH.loaded ? "All your apps (" + REACH.apps.length + ")" : "All your apps" })])]);
    all.addEventListener("click", function (e) { e.stopPropagation(); closeOpen(); appsBtn.click(); });
    return rows.concat([all]);
  }
  function peopleRow() {
    if (!(D.accessUrl || "").trim()) return null;
    var n = el("button", { class: "ab-row ab-tier-admin", type: "button",
                           "data-ab-info": "Who can sign in to " + APP_NAME + ", in what role -- add, change, deactivate or remove people." }, [
      icon("users", "ab-row-glyph"), el("span", { class: "ab-row-text" }, [el("span", { text: "People and Permissions" }),
        el("small", { text: ACCESS && ACCESS.people ? ACCESS.people.length + " people" : "" })])]);
    n.addEventListener("click", function (e) { e.stopPropagation(); openAccessBox(n); });
    return n;
  }
  function superCard() {
    if (!isSuper(realRole())) return null;
    var sel = el("select", { class: "ab-select", "aria-label": "View this app as" });
    [["", "Yourself (" + realRole() + ")"], ["Admin", "An admin"], ["Member", "A member"]].forEach(function (o) {
      sel.appendChild(el("option", { value: o[0], selected: PERSPECTIVE === o[0], text: o[1] }));
    });
    sel.addEventListener("change", function () { PERSPECTIVE = sel.value; fillDrawer(); });
    return el("div", { class: "ab-super ab-tier-admin" }, [
      el("div", { class: "ab-super-h" }, [icon("eye"), el("strong", { text: "View as" })]),
      sel,
      PERSPECTIVE ? el("small", { class: "ab-super-flag", text: "Showing what " + PERSPECTIVE.toLowerCase() +
        "s see in this drawer. The server still answers you as yourself." }) : null]);
  }
  function signOut() {
    if (!D.signout) return null;
    // A POST, always: sign-out by GET is a link any page can make a browser follow.
    var f = el("form", { class: "ab-signout", method: "post", action: D.signout }, [
      el("input", { type: "hidden", name: "csrfmiddlewaretoken", value: csrf() }),
      el("button", { class: "ab-row ab-tier-hub", type: "submit" }, [icon("logout", "ab-row-glyph"),
        el("span", { class: "ab-row-text" }, [el("span", { text: "Sign out" })])])]);
    return f;
  }
  function helpRows() {
    var tour = el("button", { class: "ab-row ab-tier-help", type: "button", "data-ab-help": "" }, [icon("tour", "ab-row-glyph"),
      el("span", { class: "ab-row-text" }, [el("span", { text: "Guided tour" })])]);
    tour.addEventListener("click", function (e) { e.stopPropagation(); closeOpen(); startTour(); });
    var about = el("button", { class: "ab-row ab-tier-help", type: "button", "data-ab-help": "" }, [icon("book", "ab-row-glyph"),
      el("span", { class: "ab-row-text" }, [el("span", { text: "About this app" }),
        D.version ? el("small", { text: vstr(D.version) }) : null])]);
    about.addEventListener("click", function (e) { e.stopPropagation(); openReadme(about); });
    return [tour, about].concat(list(island("help")).map(function (it) { return item(it, "help"); }));
  }
  function fillDrawer() {
    drawer.textContent = "";
    var head = el("div", { class: "ab-drawer-head" }, [
      el("div", { class: "ab-drawer-title" }, [el("strong", { text: VIEW === "prefs" ? "Preferences" : APP_NAME }),
        el("small", { text: VIEW === "prefs" ? "Saved to you, so they follow you" : (D.version ? vstr(D.version) : "") })]),
      VIEW === "main" ? themeToggle() : null]);
    var close = bandBtn("x", "Close");
    close.addEventListener("click", function (e) { e.stopPropagation(); closeOpen(); });
    head.appendChild(close);
    drawer.appendChild(head);
    var body = el("div", { class: "ab-drawer-body" });
    drawer.appendChild(body);
    if (VIEW === "prefs") { fillPrefs(body); return; }
    var role = effRole();
    if (PROFILE_STATE.error) body.appendChild(el("p", { class: "ab-note ab-warn", text: "Your preferences could not be saved to your record: " + PROFILE_STATE.error + ". They still apply in this browser." }));
    body.appendChild(identityCard());
    body.appendChild(section("Your apps", "hub", starredRows()));
    body.appendChild(section("", "app", list(island("profile")).map(function (it) { return item(it, "app"); })) || el("span"));
    // Component settings are offered on the HUB's word (can_edit), not the band's role -- but a
    // super admin previewing the drawer as a lower role sees what that role would.
    var cpRow = PERSPECTIVE && !isAdmin(role) ? null : componentsRow();
    if (isAdmin(role) || cpRow) {
      body.appendChild(section("Admins only", "admin", [peopleRow(), cpRow].concat(
        list(island("admin")).map(function (it) { return item(it, "admin"); }))) || el("span"));
    }
    var foot = el("div", { class: "ab-drawer-foot" });
    foot.appendChild(section("How this works", "help", helpRows()));
    var sc = superCard(); if (sc) foot.appendChild(sc);
    var so = signOut(); if (so) foot.appendChild(so);
    drawer.appendChild(foot);
  }
  avatar.addEventListener("click", function (e) {
    e.stopPropagation();
    if (openThing && openThing.kind === "drawer") { closeOpen(); return; }
    VIEW = "main";
    openAs("drawer", avatar, function () {
      drawer.hidden = true; avatar.setAttribute("aria-expanded", "false");
      if (DRAFT) { DRAFT = null; applyPrefs(); }       // an unsaved preview is discarded, never kept
    }, true);
    fillDrawer(); drawer.hidden = false; avatar.setAttribute("aria-expanded", "true");
  });

  /* ------------------------------------------------------------ preferences ----
     One line per setting, each with its own explanation. A change PREVIEWS on the page at
     once (a draft laid over the record, never written), and the scope is asked AFTER the
     change -- "Save for all apps" or "Only in <this app>" -- because nobody knows how far a
     choice should reach until they have seen it. A row this app overrides says so and can be
     put back on the everywhere-value on its own. */
  var PREF_ROWS = [
    ["theme", "Theme", [["", "App default"], ["light", "Light"], ["dark", "Dark"]],
     "Light or dark for the whole page. App default follows the app, and the app usually follows your system."],
    ["ui", "Size", [[92, "Compact"], [100, "Default"], [110, "Large"], [125, "Larger"]],
     "Scales everything on the page -- text, controls and spacing -- like browser zoom."],
    ["text", "Text", [[90, "Smaller"], [100, "Default"], [110, "Larger"], [125, "Largest"]],
     "Grows the text without growing the controls, in apps that size their text relatively."],
    ["font", "Reading face", [["", "App's own"], ["sans", "Sans"], ["serif", "Serif"], ["mono", "Mono"], ["readable", "Readable"]],
     "The typeface for reading. The app's own face stays unless you choose one; an app that names its own face keeps it."],
    ["motion", "Motion", [["full", "Full"], ["reduce", "Reduced"]],
     "Reduced turns off animation and smooth movement on the page."],
    ["tips", "Info bubbles", [["on", "On"], ["off", "Off"]],
     "Hovering a tab, a setting or an app's marked control explains what it is for."],
    ["help", "Help buttons", [["on", "On"], ["off", "Off"]],
     "The guided tour and About this app buttons, and any help button the app marks."]
  ];
  function prefChanged() {
    return !!DRAFT && Object.keys(DRAFT).some(function (k) { return String(DRAFT[k]) !== String(eff(k)); });
  }
  function fillPrefs(body) {
    var here = hereOverride();
    var rows = el("div", { class: "ab-prefs" });
    PREF_ROWS.forEach(function (r) {
      var key = r[0], current = eff(key, DRAFT);
      var seg = el("div", { class: "ab-seg", role: "radiogroup", "aria-label": r[1] });
      r[2].forEach(function (opt) {
        var on = String(current) === String(opt[0]);
        var b = el("button", { class: "ab-seg-b" + (on ? " ab-on" : ""), type: "button", role: "radio",
                               "aria-checked": on ? "true" : "false", text: opt[1] });
        b.addEventListener("click", function (e) {
          e.stopPropagation();
          DRAFT = DRAFT || {}; DRAFT[key] = opt[0];
          applyPrefs(); fillDrawer();
        });
        seg.appendChild(b);
      });
      var label = el("span", { class: "ab-pref-label" }, [el("span", { text: r[1] }),
        el("span", { class: "ab-i-dot", tabindex: "0", role: "img", "aria-label": "About " + r[1],
                     "data-ab-info": r[3], "data-ab-info-title": r[1] }, [icon("info")])]);
      var line = el("div", { class: "ab-pref" }, [label, seg]);
      if (here[key] !== undefined) {
        var reset = el("button", { class: "ab-link ab-pref-here", type: "button", text: "Only in " + APP_NAME + " · use everywhere-value",
                                   "data-ab-tip": "Put this one setting back on your everywhere-value" });
        reset.addEventListener("click", function (e) {
          e.stopPropagation();
          delete PREFS.apps[APP][key];
          var o = {}; o[APP] = {}; o[APP][key] = null;
          if (DRAFT) delete DRAFT[key];
          savePrefs({ apps: o }, function () { fillDrawer(); });
          fillDrawer();
        });
        line.appendChild(reset);
      }
      rows.appendChild(line);
    });
    body.appendChild(rows);
    var bar = el("div", { class: "ab-savebar", role: "group", "aria-label": "Save preferences" });
    if (prefChanged()) {
      var all = el("button", { class: "ab-primary", type: "button", text: "Save for all apps" });
      var only = el("button", { class: "ab-secondary", type: "button", text: "Only in " + APP_NAME });
      var discard = el("button", { class: "ab-link", type: "button", text: "Discard" });
      all.addEventListener("click", function (e) {
        e.stopPropagation();
        var payload = {}, clear = {};
        Object.keys(DRAFT).forEach(function (k) {
          PREFS[k] = DRAFT[k]; payload[k] = DRAFT[k];
          // Saved for everywhere, so this app must stop overriding it -- or the choice would
          // visibly not take here, the one place the person is looking.
          if (here[k] !== undefined) { delete PREFS.apps[APP][k]; clear[k] = null; }
        });
        if (Object.keys(clear).length) { payload.apps = {}; payload.apps[APP] = clear; }
        DRAFT = null;
        savePrefs(payload, function () { fillDrawer(); });
        fillDrawer();
      });
      only.addEventListener("click", function (e) {
        e.stopPropagation();
        PREFS.apps = PREFS.apps || {}; PREFS.apps[APP] = PREFS.apps[APP] || {};
        var o = {}; o[APP] = {};
        Object.keys(DRAFT).forEach(function (k) { PREFS.apps[APP][k] = DRAFT[k]; o[APP][k] = DRAFT[k]; });
        DRAFT = null;
        savePrefs({ apps: o }, function () { fillDrawer(); });
        fillDrawer();
      });
      discard.addEventListener("click", function (e) { e.stopPropagation(); DRAFT = {}; applyPrefs(); fillDrawer(); });
      bar.appendChild(el("span", { class: "ab-savebar-q", text: "Save this change…" }));
      bar.appendChild(all); bar.appendChild(only); bar.appendChild(discard);
    } else {
      bar.appendChild(el("span", { class: "ab-savebar-q", text: PROFILE_STATE.saving ? "Saving…"
        : PROFILE_STATE.error ? "Not saved to your record: " + PROFILE_STATE.error
        : PROFILE_STATE.lane === "absent" ? "Kept in this browser (this page has no route to your hub record)."
        : "Choose a setting to preview it here." }));
    }
    var back = el("button", { class: "ab-link", type: "button", text: "← Back" });
    back.addEventListener("click", function (e) { e.stopPropagation(); VIEW = "main"; DRAFT = null; applyPrefs(); fillDrawer(); });
    bar.appendChild(back);
    body.appendChild(bar);
  }

  /* ------------------------------------------------------------ your mark ----
     Two kinds, no third: initials, or a picture. The picture never travels as a FILE: it is
     cropped here, drawn to a 96px square and exported as a small JPEG data URL (a photo
     becomes a few KB of text on the person's record), so there is no upload lane anywhere. */
  var MARK_PX = 96, MARK_MAX_CHARS = 24000;
  var markMenu = el("div", { class: "ab-menu ab-surface", role: "menu", hidden: true });
  var fileInput = el("input", { type: "file", accept: "image/png,image/jpeg,image/webp", hidden: true, "aria-hidden": "true" });
  function openMarkMenu(anchor) {
    markMenu.textContent = "";
    var useInitials = el("button", { class: "ab-menu-row", type: "button", role: "menuitem", text: "Use my initials" });
    useInitials.addEventListener("click", function (e) {
      e.stopPropagation(); markMenu.hidden = true;
      PREFS.kind = "initials"; PREFS.image = "";
      savePrefs({ kind: "initials", image: "" }, fillDrawer); fillDrawer();
    });
    var upload = el("button", { class: "ab-menu-row", type: "button", role: "menuitem", text: "Upload a picture…" });
    upload.addEventListener("click", function (e) { e.stopPropagation(); markMenu.hidden = true; fileInput.value = ""; fileInput.click(); });
    markMenu.appendChild(useInitials); markMenu.appendChild(upload);
    var r = box(anchor);
    markMenu.hidden = false;
    markMenu.style.top = Math.round(r.bottom + 4) + "px";
    markMenu.style.left = Math.round(Math.max(6, Math.min(r.left, vw() - markMenu.offsetWidth - 6))) + "px";
  }
  doc.addEventListener("click", function (e) { if (!markMenu.hidden && !markMenu.contains(e.target)) markMenu.hidden = true; }, true);
  fileInput.addEventListener("change", function () {
    var f = fileInput.files && fileInput.files[0];
    if (!f) return;
    if (!/^image\/(png|jpeg|webp)$/.test(f.type)) { sayToast("Choose a PNG, JPEG or WebP picture."); return; }
    var url = URL.createObjectURL(f);
    var img = new Image();
    img.onload = function () { openCropper(img, url); };
    img.onerror = function () { URL.revokeObjectURL(url); sayToast("That picture could not be read."); };
    img.src = url;
  });

  var CROP = null, CROP_VIEW = 240;
  var crop = el("div", { class: "ab-crop ab-surface", role: "dialog", "aria-modal": "true", "aria-label": "Crop your picture", hidden: true });
  var cropStage = el("canvas", { class: "ab-crop-stage", width: String(CROP_VIEW), height: String(CROP_VIEW), tabindex: "0",
                                 "aria-label": "Drag to move the picture; arrow keys move it, plus and minus zoom" });
  var cropZoom = el("input", { class: "ab-crop-zoom", type: "range", min: "1", max: "4", step: "0.01", value: "1", "aria-label": "Zoom" });
  var cropMsg = el("p", { class: "ab-note", role: "status" });
  var cropSave = el("button", { class: "ab-primary", type: "button", text: "Use this picture" });
  var cropCancel = el("button", { class: "ab-link", type: "button", text: "Cancel" });
  crop.appendChild(el("div", { class: "ab-crop-head" }, [el("strong", { text: "Your picture" }),
    el("small", { text: "The circle is what every app shows." })]));
  crop.appendChild(el("div", { class: "ab-crop-frame" }, [cropStage]));
  crop.appendChild(el("label", { class: "ab-crop-zl" }, [el("span", { text: "Zoom" }), cropZoom]));
  crop.appendChild(cropMsg);
  crop.appendChild(el("div", { class: "ab-crop-foot" }, [cropCancel, cropSave]));
  function cropBase() { return Math.max(CROP_VIEW / CROP.img.naturalWidth, CROP_VIEW / CROP.img.naturalHeight); }
  function cropClamp() {
    var s = cropBase() * CROP.zoom, w = CROP.img.naturalWidth * s, h = CROP.img.naturalHeight * s;
    CROP.x = Math.min(0, Math.max(CROP_VIEW - w, CROP.x));
    CROP.y = Math.min(0, Math.max(CROP_VIEW - h, CROP.y));
  }
  function cropPaint() {
    var ctx = cropStage.getContext("2d"), s = cropBase() * CROP.zoom;
    ctx.clearRect(0, 0, CROP_VIEW, CROP_VIEW);
    ctx.drawImage(CROP.img, CROP.x, CROP.y, CROP.img.naturalWidth * s, CROP.img.naturalHeight * s);
  }
  /* Zoom about a POINT (the pointer, or the middle for the slider and keys), not about the
     corner: somebody who has dragged a face to the edge must not lose it by zooming. */
  function cropSetZoom(z, px, py) {
    var old = cropBase() * CROP.zoom;
    CROP.zoom = Math.max(1, Math.min(4, z));
    var s = cropBase() * CROP.zoom;
    px = px === undefined ? CROP_VIEW / 2 : px; py = py === undefined ? CROP_VIEW / 2 : py;
    CROP.x = px - (px - CROP.x) * s / old; CROP.y = py - (py - CROP.y) * s / old;
    cropZoom.value = String(CROP.zoom); cropClamp(); cropPaint();
  }
  function openCropper(img, url) {
    closeOpen();
    markMenu.hidden = true;
    CROP = { img: img, url: url, zoom: 1, x: 0, y: 0, drag: null };
    var s = cropBase();
    CROP.x = (CROP_VIEW - img.naturalWidth * s) / 2; CROP.y = (CROP_VIEW - img.naturalHeight * s) / 2;
    cropZoom.value = "1"; cropMsg.textContent = "";
    openAs("crop", avatar, function () { crop.hidden = true; if (CROP) URL.revokeObjectURL(CROP.url); CROP = null; }, true);
    crop.hidden = false; cropPaint();
    try { cropStage.focus(); } catch (e) {}
  }
  cropStage.addEventListener("pointerdown", function (e) {
    if (!CROP) return;
    cropStage.setPointerCapture(e.pointerId);
    CROP.drag = { x: e.clientX, y: e.clientY, ox: CROP.x, oy: CROP.y };
  });
  cropStage.addEventListener("pointermove", function (e) {
    if (!CROP || !CROP.drag) return;
    var k = CROP_VIEW / cropStage.getBoundingClientRect().width;     // CSS px -> stage px
    CROP.x = CROP.drag.ox + (e.clientX - CROP.drag.x) * k; CROP.y = CROP.drag.oy + (e.clientY - CROP.drag.y) * k;
    cropClamp(); cropPaint();
  });
  cropStage.addEventListener("pointerup", function () { if (CROP) CROP.drag = null; });
  cropStage.addEventListener("wheel", function (e) {
    if (!CROP) return;
    e.preventDefault();
    var r = cropStage.getBoundingClientRect(), k = CROP_VIEW / r.width;
    cropSetZoom(CROP.zoom * (e.deltaY < 0 ? 1.08 : 1 / 1.08), (e.clientX - r.left) * k, (e.clientY - r.top) * k);
  }, { passive: false });
  cropStage.addEventListener("keydown", function (e) {
    if (!CROP) return;
    var step = e.shiftKey ? 24 : 6, done = true;
    if (e.key === "ArrowLeft") CROP.x -= step; else if (e.key === "ArrowRight") CROP.x += step;
    else if (e.key === "ArrowUp") CROP.y -= step; else if (e.key === "ArrowDown") CROP.y += step;
    else if (e.key === "+" || e.key === "=") { cropSetZoom(CROP.zoom * 1.1); return e.preventDefault(); }
    else if (e.key === "-") { cropSetZoom(CROP.zoom / 1.1); return e.preventDefault(); }
    else done = false;
    if (done) { e.preventDefault(); cropClamp(); cropPaint(); }
  });
  cropZoom.addEventListener("input", function () { if (CROP) cropSetZoom(Number(cropZoom.value)); });
  cropCancel.addEventListener("click", function (e) { e.stopPropagation(); closeOpen(); });
  cropSave.addEventListener("click", function (e) {
    e.stopPropagation();
    if (!CROP) return;
    var out = el("canvas", { width: String(MARK_PX), height: String(MARK_PX) });
    var ctx = out.getContext("2d");
    ctx.fillStyle = "#ffffff"; ctx.fillRect(0, 0, MARK_PX, MARK_PX);     // JPEG has no alpha
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(cropStage, 0, 0, CROP_VIEW, CROP_VIEW, 0, 0, MARK_PX, MARK_PX);
    var data = out.toDataURL("image/jpeg", 0.82);
    if (data.length > MARK_MAX_CHARS) { cropMsg.textContent = "That picture is still too large after cropping; try a simpler one."; return; }
    PREFS.kind = "image"; PREFS.image = data;
    closeOpen();
    savePrefs({ kind: "image", image: data }, function (ok) { if (!ok) sayToast("Your picture is shown here but was not saved: " + PROFILE_STATE.error); });
  });

  /* ---------------------------------------------- people and permissions ----
     The whole access page, as a box over the app, speaking ONE documented contract at
     data-access-url (patterns/app-banner.md):
       GET            {roles:[{value,label,what}], default_role, people:[{id, username, display,
                       role, active, self, added_by}], events:[{when, actor, action, target}]}
       GET ?q=<text>  {people:[{username, display, subtitle, known}], error, unconfigured}
       POST {action:"add", people:[{username, display}], role}  -> {ok, outcomes:{added,updated,unchanged,refused}}
       POST {action:"set", id, role} | {action:"active", id, active} | {action:"delete", id}  -> {ok, detail|reason}
     Optional parts are drawn only when they came back. The box NEVER enforces a rule itself:
     self-demotion, self-removal and the last-admin guard are the SERVER's, and its refusal is
     shown verbatim. POSTs carry X-CSRFToken; a refusal that is not JSON still names its HTTP
     status instead of reading as "nothing happened". */
  var ACCESS = null, ACCESS_ERR = "", ACCESS_MSG = [], ACCESS_BUSY = false, PP = null;
  var ppBox = el("div", { class: "ab-box ab-pp ab-surface", role: "dialog", "aria-modal": "true",
                          "aria-label": "People and Permissions", hidden: true });
  function accessUrl() { return (D.accessUrl || "").trim(); }
  function ppNote(tone, text) { ACCESS_MSG.push({ tone: tone, text: text }); }
  function accessLoad(then) {
    if (!accessUrl()) return;
    fetch(accessUrl(), { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { if (!r.ok) throw new Error("the app answered HTTP " + r.status); return r.json(); })
      .then(function (b) { if (!b || !Array.isArray(b.people)) throw new Error("the answer carried no roster"); ACCESS = b; ACCESS_ERR = ""; if (then) then(); })
      .catch(function (err) { ACCESS_ERR = "Could not read this app's roster (" + String(err && err.message || err) + ")."; if (then) then(); });
  }
  function ppRoles() { return list(ACCESS && ACCESS.roles); }
  function ppRoleLabel(v) { var hit = ppRoles().filter(function (r) { return r.value === v; })[0]; return hit ? hit.label : v; }
  function ppDefaultRole() {
    if (ACCESS && ACCESS.default_role) return ACCESS.default_role;
    var roles = ppRoles();
    return (roles[0] && roles[0].value) || "";
  }
  function accessSend(payload, done) {
    if (ACCESS_BUSY) {
      // Said, never dropped: a press that did nothing and said nothing reads as "it worked".
      ACCESS_MSG = [{ tone: "info", text: "Still saving the previous change — press again in a moment." }];
      ppPaint(); return;
    }
    ACCESS_BUSY = true; ACCESS_MSG = []; ppNote("info", "Working…"); ppPaint();
    fetch(accessUrl(), { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": csrf() },
      body: JSON.stringify(payload) })
      .then(function (r) {
        return r.json().then(function (b) { return { ok: r.ok, body: b || {} }; },
          function () { return { ok: r.ok, body: r.ok ? {} : { reason: "the app answered HTTP " + r.status } }; });
      })
      .then(function (res) {
        ACCESS_MSG = [];
        var b = res.body, o = b.outcomes;
        if (o) {
          var label = ppRoleLabel(payload.role);
          if (list(o.added).length) ppNote("ok", "Added as " + label + ": " + o.added.join(", ") + ".");
          if (list(o.updated).length) ppNote("ok", "Changed to " + label + ": " + o.updated.join(", ") + ".");
          if (list(o.unchanged).length) ppNote("info", "Already had exactly this access, nothing changed: " + o.unchanged.join(", ") + ".");
          if (list(o.refused).length) ppNote("error", "Not changed: " + o.refused.join("; ") + ".");
        } else {
          ppNote(res.ok ? "ok" : "error", b.detail || b.reason || (res.ok ? "Done." : "That was refused."));
        }
        ACCESS_BUSY = false;
        if (done) done(res.ok);
        accessLoad(ppPaint);
      })
      .catch(function () { ACCESS_MSG = []; ppNote("error", "The roster did not answer."); ACCESS_BUSY = false; ppPaint(); });
  }
  function ppSelect(value, label, onPick, disabled) {
    var sel = el("select", { class: "ab-select", "aria-label": label, disabled: !!disabled });
    ppRoles().forEach(function (r) { sel.appendChild(el("option", { value: r.value, title: r.what || null, selected: r.value === value, text: r.label })); });
    sel.addEventListener("change", function () { onPick(sel.value); });
    return sel;
  }
  function ppWhen(v) { if (!v) return "—"; var d = new Date(v); return isNaN(d.getTime()) ? String(v) : d.toLocaleString(); }
  function ppCard(title, kids) {
    return el("section", { class: "ab-pp-card" }, [title ? el("h3", { class: "ab-sec-h", text: title }) : null].concat(kids || []));
  }
  function placeUnderChrome(target) {
    var bottom = 0;
    try { bottom = Math.max(0, box(mount).bottom); } catch (e) {}
    target.style.setProperty("--ab-box-top", Math.round(Math.min(bottom, vh() * 0.4)) + 12 + "px");
  }
  function openAccessBox(anchor) {
    if (!accessUrl()) return;
    closeOpen();
    ACCESS_MSG = [];
    PP = { queued: [], found: null, query: "", timer: 0, confirm: null };
    ppBuild(); placeUnderChrome(ppBox);
    openAs("access", anchor, function () { ppBox.hidden = true; if (PP && PP.timer) clearTimeout(PP.timer); PP = null; }, true);
    ppBox.hidden = false;
    accessLoad(ppPaint);
    try { PP.input.focus(); } catch (e) {}
  }
  function ppBuild() {
    ppBox.textContent = "";
    var close = bandBtn("x", "Close");
    close.addEventListener("click", function (e) { e.stopPropagation(); closeOpen(); });
    PP.sub = el("small");
    ppBox.appendChild(el("div", { class: "ab-box-head" }, [el("div", null, [el("strong", { text: "Who can sign in to " + APP_NAME }), PP.sub]), close]));
    var body = el("div", { class: "ab-box-body" });
    PP.msg = el("div", { class: "ab-pp-msgs", role: "status", "aria-live": "polite" });
    body.appendChild(PP.msg);
    // Add people: ONE line of people, ONE role for all of them, ONE submit -- choosing the
    // role per person is how somebody ends up an admin by accident.
    PP.line = el("div", { class: "ab-pp-line" });
    PP.input = el("input", { class: "ab-input", type: "search", autocomplete: "off", placeholder: "Type a name to find someone",
                             "aria-label": "Find a person in the directory" });
    PP.input.addEventListener("input", function () {
      PP.query = PP.input.value.trim();
      clearTimeout(PP.timer);
      PP.timer = setTimeout(ppSearch, 250);
    });
    PP.input.addEventListener("keydown", function (e) { if (e.key !== "Escape") e.stopPropagation(); });
    PP.results = el("div", { class: "ab-pp-results" });
    PP.addRole = el("span");
    PP.addBtn = el("button", { class: "ab-primary", type: "button", text: "Add" });
    PP.addBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      if (!PP.queued.length) return;
      var role = PP.addRoleSel ? PP.addRoleSel.value : ppDefaultRole();
      accessSend({ action: "add", people: PP.queued.slice(), role: role }, function (ok) { if (ok) { PP.queued = []; ppPaintQueue(); } });
    });
    body.appendChild(ppCard("Add people", [PP.line, PP.input, PP.results, el("div", { class: "ab-pp-addbar" }, [el("span", { text: "as" }), PP.addRole, PP.addBtn])]));
    // Paste instead: the fallback when the directory search is not configured.
    PP.paste = el("textarea", { class: "ab-input", rows: "2", placeholder: "alice, bob@example.com", "aria-label": "Usernames to add" });
    PP.paste.addEventListener("keydown", function (e) { if (e.key !== "Escape") e.stopPropagation(); });
    PP.pasteRole = el("span");
    var pasteBtn = el("button", { class: "ab-secondary", type: "button", text: "Add these" });
    pasteBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      var names = PP.paste.value.split(/[\s,;]+/).map(function (s) { return s.trim(); }).filter(Boolean);
      if (!names.length) return;
      accessSend({ action: "add", people: names.map(function (n) { return { username: n, display: n }; }),
                   role: PP.pasteRoleSel ? PP.pasteRoleSel.value : ppDefaultRole() },
        function (ok) { if (ok) PP.paste.value = ""; });
    });
    var pasteBox = el("details", { class: "ab-pp-paste" }, [el("summary", { text: "Paste usernames instead" }), PP.paste,
      el("div", { class: "ab-pp-addbar" }, [el("span", { text: "as" }), PP.pasteRole, pasteBtn])]);
    body.appendChild(pasteBox);
    PP.roster = el("div", { class: "ab-pp-roster" });
    body.appendChild(ppCard("People", [PP.roster]));
    PP.ladder = el("div");
    body.appendChild(PP.ladder);
    PP.events = el("div");
    body.appendChild(PP.events);
    ppBox.appendChild(body);
  }
  function ppSearch() {
    if (!PP) return;
    if (PP.query.length < 2) { PP.found = null; ppPaintResults(); return; }
    var asked = PP.query;
    fetch(accessUrl() + (accessUrl().indexOf("?") >= 0 ? "&" : "?") + "q=" + encodeURIComponent(asked),
          { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (b) { if (!PP || PP.query !== asked) return; PP.found = b || {}; ppPaintResults(); })
      .catch(function (err) { if (!PP) return; PP.found = { error: "The directory did not answer (" + err.message + ")." }; ppPaintResults(); });
  }
  function ppPaintResults() {
    PP.results.textContent = "";
    var f = PP.found;
    if (!f) return;
    if (f.unconfigured) { PP.results.appendChild(el("p", { class: "ab-note", text: "This app has no directory search. Paste usernames instead (below)." })); return; }
    if (f.error) { PP.results.appendChild(el("p", { class: "ab-note ab-warn", text: f.error })); return; }
    var people = list(f.people);
    if (!people.length) { PP.results.appendChild(el("p", { class: "ab-note", text: "Nobody in the directory matches “" + PP.query + "”." })); return; }
    people.slice(0, 8).forEach(function (p) {
      var queued = PP.queued.some(function (x) { return x.username === p.username; });
      var b = el("button", { class: "ab-pp-hit", type: "button", disabled: queued || !!p.known }, [
        el("strong", { text: p.display || p.username }),
        el("small", { text: [p.username, p.subtitle, p.known ? "already has access" : ""].filter(Boolean).join(" · ") })]);
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        PP.queued.push({ username: p.username, display: p.display || p.username });
        PP.input.value = ""; PP.query = ""; PP.found = null;
        ppPaintQueue(); ppPaintResults(); PP.input.focus();
      });
      PP.results.appendChild(b);
    });
  }
  function ppPaintQueue() {
    PP.line.textContent = "";
    PP.queued.forEach(function (p, i) {
      var x = el("button", { class: "ab-pp-pill", type: "button", "aria-label": "Remove " + p.display + " from the line" }, [
        el("span", { text: p.display }), icon("x")]);
      x.addEventListener("click", function (e) { e.stopPropagation(); PP.queued.splice(i, 1); ppPaintQueue(); });
      PP.line.appendChild(x);
    });
    PP.addBtn.disabled = !PP.queued.length || ACCESS_BUSY;
    PP.addBtn.textContent = PP.queued.length > 1 ? "Add " + PP.queued.length + " people" : "Add";
  }
  function ppPaint() {
    if (!PP) return;
    PP.msg.textContent = "";
    if (ACCESS_ERR) PP.msg.appendChild(el("p", { class: "ab-note ab-warn", text: ACCESS_ERR }));
    ACCESS_MSG.forEach(function (m) { PP.msg.appendChild(el("p", { class: "ab-note ab-tone-" + m.tone, text: m.text })); });
    var people = list(ACCESS && ACCESS.people);
    PP.sub.textContent = ACCESS ? people.length + " people · " + people.filter(function (p) { return p.active !== false; }).length + " active" : "";
    PP.addRole.textContent = ""; PP.pasteRole.textContent = "";
    if (ACCESS) {
      PP.addRoleSel = ppSelect(PP.addRoleSel ? PP.addRoleSel.value : ppDefaultRole(), "Role for the people on the line", function () {});
      PP.pasteRoleSel = ppSelect(PP.pasteRoleSel ? PP.pasteRoleSel.value : ppDefaultRole(), "Role for the pasted people", function () {});
      PP.addRole.appendChild(PP.addRoleSel); PP.pasteRole.appendChild(PP.pasteRoleSel);
    }
    ppPaintQueue();
    PP.roster.textContent = "";
    if (!ACCESS) { PP.roster.appendChild(el("p", { class: "ab-note", text: ACCESS_ERR ? "" : "Reading the roster…" })); }
    people.forEach(function (p) {
      var self = !!p.self;
      var row = el("div", { class: "ab-pp-row" + (p.active === false ? " ab-off" : "") }, [
        el("div", { class: "ab-pp-who" }, [el("strong", { text: (p.display || p.username) + (self ? " (you)" : "") }),
          el("small", { text: [p.username, p.active === false ? "deactivated" : "", p.added_by ? "added by " + p.added_by : ""].filter(Boolean).join(" · ") })]),
        ppSelect(p.role, "Role for " + (p.display || p.username), function (v) { accessSend({ action: "set", id: p.id, role: v }); }, self)]);
      var act = el("button", { class: "ab-link", type: "button", disabled: self, text: p.active === false ? "Reactivate" : "Deactivate" });
      act.addEventListener("click", function (e) { e.stopPropagation(); accessSend({ action: "active", id: p.id, active: p.active === false }); });
      row.appendChild(act);
      // Remove is irreversible, so it takes two presses and says what the reversible way is.
      if (PP.confirm === p.id) {
        var yes = el("button", { class: "ab-danger", type: "button", text: "Remove for good" });
        yes.addEventListener("click", function (e) { e.stopPropagation(); PP.confirm = null; accessSend({ action: "delete", id: p.id }); });
        var no = el("button", { class: "ab-link", type: "button", text: "Keep" });
        no.addEventListener("click", function (e) { e.stopPropagation(); PP.confirm = null; ppPaint(); });
        row.appendChild(el("div", { class: "ab-pp-confirm" }, [el("small", { text: "Removing deletes their access record. Deactivate is the reversible way." }), yes, no]));
      } else {
        var rm = el("button", { class: "ab-link ab-warn", type: "button", disabled: self, text: "Remove" });
        rm.addEventListener("click", function (e) { e.stopPropagation(); PP.confirm = p.id; ppPaint(); });
        row.appendChild(rm);
      }
      PP.roster.appendChild(row);
    });
    PP.ladder.textContent = "";
    if (ppRoles().some(function (r) { return r.what; })) {
      PP.ladder.appendChild(ppCard("What each role can do", ppRoles().map(function (r) {
        return el("p", { class: "ab-pp-rung" }, [el("strong", { text: r.label }), el("span", { text: " — " + (r.what || "") })]);
      })));
    }
    PP.events.textContent = "";
    if (ACCESS && Array.isArray(ACCESS.events)) {
      PP.events.appendChild(ppCard("Recent access changes", ACCESS.events.length ? ACCESS.events.slice(0, 12).map(function (ev) {
        return el("p", { class: "ab-pp-ev" }, [el("small", { text: ppWhen(ev.when) }), el("span", { text: " " + [ev.actor, ev.action, ev.target].filter(Boolean).join(" ") })]);
      }) : [el("p", { class: "ab-note", text: "No changes recorded yet." })]));
    }
  }
  win.addEventListener("resize", function () { if (PP) placeUnderChrome(ppBox); });

  /* ------------------------------------------------- guided tour and read-me ----
     DERIVED, NOT AUTHORED: both are built at the moment they are OPENED, from the page as it
     is right then. A step exists only because its target is on screen, so renaming a page or
     dropping a control updates the tour with nobody maintaining it. An app that writes its own
     still wins (data-tour-url: {steps:[{selector,title,text}]}; data-readme-url:
     {lead, sections:[{head, lines}], note}) -- and a declared step whose selector finds
     nothing is NAMED in the console and skipped, so a tour cannot narrate a removed button. */
  var TOUR = { on: false, steps: [], at: 0, declared: null };
  var README_DOC = null;
  function bandTour() {
    var spec = [
      [".ab-home", APP_NAME, "The app's name. It takes you home from anywhere in " + APP_NAME + "."],
      [".ab-strip, .ab-sb", "The pages of this app", "Everything " + APP_NAME + " can show you. The page you are on is marked."],
      [".ab-slot:not(.ab-slot--empty)", "This app's own controls", "The things only " + APP_NAME + " has. Everything right of the line works the same in every app."],
      [".ab-controls .ab-btn[aria-label='Your apps']", "Your apps", "The other apps you can open. Star the ones you use most."],
      [".ab-avatar", "You", "Your preferences, your icon, help, and Sign out are all behind here."]
    ];
    return spec.map(function (s) { var n = q(s[0]); return vis(n) ? { node: n, title: s[1], text: s[2] } : null; }).filter(Boolean);
  }
  function pageTour() {
    // The app's own KEY THINGS, as the app marked them for info bubbles: a marked thing that
    // is on screen is a step, outside the band's own chrome, at most eight.
    return Array.prototype.filter.call(doc.querySelectorAll("[data-ab-info]"), function (n) {
      return vis(n) && !n.closest(".ab-root, .ab-surface");
    }).slice(0, 8).map(function (n) { return { node: n, title: infoName(n), text: n.getAttribute("data-ab-info") }; });
  }
  function declaredTour(steps) {
    var out = [], missing = [];
    steps.forEach(function (st) {
      var sel = st.selector || st.target, n = sel ? q(sel) : null;
      if (vis(n)) out.push({ node: n, title: st.title || "", text: st.text || "" });
      else missing.push(sel || "(no selector)");
    });
    if (missing.length) console.warn(LOG + "guided tour: " + missing.length + " declared step(s) point at nothing on this page and were skipped: " + missing.join(", "));
    return out;
  }
  var tourScrim = el("div", { class: "ab-tour-scrim ab-surface", hidden: true });
  var tourRing = el("div", { class: "ab-tour-ring ab-surface", hidden: true });
  var tourCard = el("div", { class: "ab-tour ab-surface", role: "dialog", "aria-modal": "true", "aria-label": "Guided tour", hidden: true });
  var tourTitle = el("strong", { class: "ab-tour-title" });
  var tourText = el("p", { class: "ab-tour-text" });
  var tourCount = el("small", { class: "ab-tour-count" });
  var tourPrev = el("button", { class: "ab-secondary", type: "button", text: "Back" });
  var tourNext = el("button", { class: "ab-primary", type: "button", text: "Next" });
  var tourEnd = bandBtn("x", "End the tour");
  tourCard.appendChild(el("div", { class: "ab-tour-head" }, [tourTitle, tourEnd]));
  tourCard.appendChild(tourText);
  tourCard.appendChild(el("div", { class: "ab-tour-foot" }, [tourCount, el("span", null, [tourPrev, tourNext])]));
  function tourPlace() {
    var st = TOUR.steps[TOUR.at];
    if (!st) { endTour(); return; }
    var r = box(st.node);                                 // re-read: the page does not hold still
    if (!r.width && !r.height) { tourGo(1); return; }
    var pad = 6;
    tourRing.style.top = r.top - pad + "px"; tourRing.style.left = r.left - pad + "px";
    tourRing.style.width = r.width + pad * 2 + "px"; tourRing.style.height = r.height + pad * 2 + "px";
    tourTitle.textContent = st.title; tourText.textContent = st.text;
    tourCount.textContent = TOUR.at + 1 + " of " + TOUR.steps.length;
    tourPrev.disabled = TOUR.at === 0;
    tourNext.textContent = TOUR.at === TOUR.steps.length - 1 ? "Done" : "Next";
    var cw = Math.min(360, vw() - 24);
    tourCard.style.width = cw + "px";
    var below = r.bottom + 12, fits = below + tourCard.offsetHeight < vh() - 12;
    tourCard.style.top = (fits ? below : Math.max(12, r.top - tourCard.offsetHeight - 12)) + "px";
    tourCard.style.left = Math.max(12, Math.min(r.left, vw() - cw - 12)) + "px";
  }
  function tourGo(d) {
    var next = TOUR.at + d;
    if (next < 0) return;
    if (next >= TOUR.steps.length) { endTour(); return; }
    TOUR.at = next; tourPlace();
  }
  function tourKeys(e) {
    if (!TOUR.on) return;
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); endTour(); }
    else if (e.key === "ArrowRight") { e.preventDefault(); tourGo(1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); tourGo(-1); }
  }
  function startTour() {
    // IDEMPOTENT: pressing the button while the tour runs restarts it, never layers a second
    // scrim over the first until the page goes black.
    closeOpen();
    TOUR.steps = (TOUR.declared ? declaredTour(TOUR.declared) : []).concat(bandTour(), TOUR.declared ? [] : pageTour());
    if (!TOUR.steps.length) return;
    TOUR.at = 0;
    if (!TOUR.on) {
      TOUR.on = true;
      tourScrim.hidden = tourRing.hidden = tourCard.hidden = false;
      doc.addEventListener("keydown", tourKeys, true);
      win.addEventListener("resize", tourPlace); win.addEventListener("scroll", tourPlace, true);
    }
    tourPlace();
    try { tourNext.focus(); } catch (e) {}
  }
  function endTour() {
    if (!TOUR.on) return;
    TOUR.on = false;
    tourScrim.hidden = tourRing.hidden = tourCard.hidden = true;
    doc.removeEventListener("keydown", tourKeys, true);
    win.removeEventListener("resize", tourPlace); win.removeEventListener("scroll", tourPlace, true);
  }
  tourPrev.addEventListener("click", function () { tourGo(-1); });
  tourNext.addEventListener("click", function () { tourGo(1); });
  tourEnd.addEventListener("click", endTour);
  tourScrim.addEventListener("click", endTour);
  tourBtn.addEventListener("click", function (e) { e.stopPropagation(); startTour(); });

  /* ------------------------------------------------- component settings ----
     What the operator sets for the hosted components on THIS app -- not a person's choice
     (that is Preferences) but the app's, for everyone who uses it: the agent's welcome and
     suggested questions, the search bar's pre-text, which of the app's own controls come off
     the band. The hub stores them per app (hub_core/components.py) and serves them beside the
     components, with the SCHEMA each component's manifest declares:

       GET  {hub}/components/props/<app>.json   {props, schema, can_edit, updated_at[, updated_by, history]}
       POST {hub}/api/component-props           {app, props}  (CSRF; the hub re-decides who may)

     The banner reads them on every page and HANDS THEM ON: window.HubComponentProps for a
     component that loads after it, and a `hub:component-props` window event for one already
     running ({app, props, preview}). No component reaches into another.

     The editor is drawn FROM THE SCHEMA, so a component that declares a new property brings
     its own control with no change here. It is offered only when the hub says this reader
     can_edit (the adopter's HUB_COMPONENT_EDITOR decides; the band never does). A change is
     PREVIEWED on this page at once -- the same event, marked preview -- and only Save makes it
     everyone's; closing the box, Discard or Escape put the page back on what is saved. */
  var CP_URL = (D.props || (HUB + "/components/props/" + encodeURIComponent(APP) + ".json"));
  var CP_SAVE_URL = HUB + "/api/component-props";
  var CP = null, CP_META = null, CP_ERR = "", CP_DRAFT = null, CP_TAB = "";
  var CP_SAVE = { busy: false, note: "", error: "" };
  var cpBox = el("div", { class: "ab-box ab-cp ab-surface", role: "dialog", "aria-modal": "true",
                          "aria-label": "Component settings", hidden: true });
  var cpFoot = null;
  function cpClone(o) { return JSON.parse(JSON.stringify(o || {})); }
  function cpSchema() { return list(CP_META && CP_META.schema); }
  function cpVal(props, comp, f) {
    var c = props && props[comp];
    return c && c[f.key] !== undefined && c[f.key] !== null ? c[f.key] : f["default"];
  }
  function cpSame(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
  function cpDirty() { return !!CP_DRAFT && !cpSame(CP_DRAFT, CP || {}); }
  function publishProps(props, preview) {
    var detail = { app: APP, props: props || {}, preview: !!preview };
    try { win.HubComponentProps = detail; } catch (e) {}
    try { win.dispatchEvent(new CustomEvent("hub:component-props", { detail: detail })); } catch (e) {}
  }
  function cpUrl() { return CP_URL + (CP_URL.indexOf("?") >= 0 ? "&" : "?") + "component=banner"; }
  function cpLoad(then) {
    fetch(cpUrl(), { credentials: "same-origin", cache: "no-cache", headers: { Accept: "application/json" } })
      .then(function (r) {
        return r.json().then(function (b) {
          if (!r.ok || !b || b.ok === false) throw new Error((b && b.reason) || "the hub answered HTTP " + r.status);
          return b;
        }, function () { throw new Error("the hub answered HTTP " + r.status + " without JSON"); });
      })
      .then(function (b) {
        CP_META = b; CP_ERR = ""; CP = b.props || {};
        if (!CP_DRAFT) publishProps(CP, false);
        if (then) then();
      })
      .catch(function (err) {
        // Said where it is looked for -- the editor -- and never on the band: the components
        // keep their own defaults, which is exactly what an app with no settings looks like.
        CP_ERR = String(err && err.message || err);
        console.warn(LOG + "component settings could not be read: " + CP_ERR);
        if (then) then();
      });
  }
  function componentsRow() {
    if (!(CP_META && CP_META.can_edit) || !cpSchema().length) return null;
    var n = el("button", { class: "ab-row ab-tier-admin", type: "button",
                           "data-ab-info": "What the shared components show on " + APP_NAME + " for everyone -- the agent's welcome, the search bar's pre-text, controls taken off the band. Previewed here before you save." }, [
      icon("sliders", "ab-row-glyph"), el("span", { class: "ab-row-text" }, [el("span", { text: "Component settings" }),
        el("small", { text: cpSchema().length + " component" + (cpSchema().length === 1 ? "" : "s") })])]);
    n.addEventListener("click", function (e) { e.stopPropagation(); openComponentBox(n); });
    return n;
  }
  function openComponentBox(anchor) {
    closeOpen();
    CP_DRAFT = cpClone(CP);
    CP_SAVE = { busy: false, note: "", error: "" };
    if (!CP_TAB && cpSchema().length) CP_TAB = cpSchema()[0].component;
    placeUnderChrome(cpBox);
    openAs("components", anchor, function () {
      cpBox.hidden = true;
      // An unsaved preview never outlives the person looking at it.
      if (CP_DRAFT) { CP_DRAFT = null; publishProps(CP, false); }
    }, true);
    cpBox.hidden = false;
    cpFill();
    // Read afresh: somebody else may have saved since this page loaded, and a draft built on
    // a stale record would put their change back.
    cpLoad(function () {
      if (!(openThing && openThing.kind === "components")) return;
      if (!cpDirty()) CP_DRAFT = cpClone(CP);
      cpFill();
    });
  }
  function cpSet(comp, key, value) {
    CP_DRAFT[comp] = CP_DRAFT[comp] || {};
    CP_DRAFT[comp][key] = value;
    CP_SAVE.note = ""; CP_SAVE.error = "";
    publishProps(CP_DRAFT, true);
    cpPaintFoot();
  }
  function cpField(comp, f) {
    var value = cpVal(CP_DRAFT, comp, f), id = "ab-cp-" + comp + "-" + f.key;
    var opts = list(f.options);
    var control;
    if (f.type === "choice" && opts.length <= 4) {
      control = el("div", { class: "ab-seg", role: "radiogroup", "aria-labelledby": id + "-l" });
      opts.forEach(function (o) {
        var on = String(value) === String(o.value);
        var b = el("button", { class: "ab-seg-b" + (on ? " ab-on" : ""), type: "button", role: "radio",
                               "aria-checked": on ? "true" : "false", text: o.label || o.value });
        b.addEventListener("click", function (e) {
          e.stopPropagation();
          Array.prototype.forEach.call(control.children, function (x) { x.classList.remove("ab-on"); x.setAttribute("aria-checked", "false"); });
          b.classList.add("ab-on"); b.setAttribute("aria-checked", "true");
          cpSet(comp, f.key, o.value);
        });
        control.appendChild(b);
      });
    } else if (f.type === "choice") {
      control = el("select", { class: "ab-select", id: id });
      opts.forEach(function (o) { control.appendChild(el("option", { value: o.value, selected: String(value) === String(o.value), text: o.label || o.value })); });
      control.addEventListener("change", function () { cpSet(comp, f.key, control.value); });
    } else if (f.type === "multi") {
      control = el("div", { class: "ab-cp-multi", role: "group", "aria-labelledby": id + "-l" });
      var picked = list(value).slice();
      var all = el("button", { class: "ab-link", type: "button" });
      var paintAll = function () { all.textContent = picked.length === opts.length ? "Clear all" : "Select all"; };
      var boxes = opts.map(function (o) {
        var cb = el("input", { type: "checkbox", checked: picked.indexOf(o.value) >= 0 });
        cb.addEventListener("change", function () {
          picked = opts.filter(function (x, i) { return boxes[i].firstChild.checked; }).map(function (x) { return x.value; });
          cpSet(comp, f.key, picked); paintAll();
        });
        return el("label", { class: "ab-cp-check" }, [cb, el("span", { text: o.label || o.value })]);
      });
      // A select-all toggle, because "every one of them" is the common answer and a row of
      // clicks to say it invites a missed box.
      all.addEventListener("click", function (e) {
        e.stopPropagation();
        var on = picked.length !== opts.length;
        boxes.forEach(function (b) { b.firstChild.checked = on; });
        picked = on ? opts.map(function (x) { return x.value; }) : [];
        cpSet(comp, f.key, picked); paintAll();
      });
      paintAll();
      boxes.forEach(function (b) { control.appendChild(b); });
      control.appendChild(all);
    } else {
      // text and list. A list is one item per line; the count is shown against its cap so the
      // hub's refusal of one item too many is never the first anyone hears of the limit.
      var isList = f.type === "list", lines = parseInt(f.lines || (isList ? 4 : 1), 10) || 1;
      var max = parseInt(f.max || 200, 10), maxItems = parseInt(f.max_items || 8, 10);
      var input = lines > 1 || isList
        ? el("textarea", { class: "ab-input ab-cp-text", id: id, rows: String(Math.max(lines, isList ? Math.min(maxItems, 6) : 2)) })
        : el("input", { class: "ab-input ab-cp-text", id: id, type: "text", maxlength: String(max) });
      input.value = isList ? list(value).join("\n") : String(value || "");
      var count = el("small", { class: "ab-cp-count" });
      var items = function () { return input.value.split("\n").map(function (s) { return s.trim(); }).filter(Boolean); };
      var paintCount = function () {
        if (isList) {
          var got = items(), long = got.filter(function (s) { return s.length > max; }).length;
          count.textContent = got.length + " of " + maxItems + (long ? " · " + long + " longer than " + max + " characters" : "");
          count.classList.toggle("ab-warn", got.length > maxItems || long > 0);
        } else {
          count.textContent = input.value.length + " of " + max;
          count.classList.toggle("ab-warn", input.value.length > max);
        }
      };
      input.addEventListener("input", function () { cpSet(comp, f.key, isList ? items() : input.value); paintCount(); });
      paintCount();
      control = el("div", { class: "ab-cp-textwrap" }, [input, count]);
    }
    var reset = null;
    if (!cpSame(value, f["default"])) {
      reset = el("button", { class: "ab-link", type: "button", text: "Back to default" });
      reset.addEventListener("click", function (e) { e.stopPropagation(); cpSet(comp, f.key, cpClone(f["default"])); cpFill(); });
    }
    return el("div", { class: "ab-pref ab-cp-field" }, [
      el("span", { class: "ab-pref-label", id: id + "-l" }, [el("label", { "for": id, text: f.label || f.key }), reset]),
      f.hint ? el("small", { class: "ab-cp-hint", text: f.hint }) : null,
      control]);
  }
  function cpWhen(v) { if (!v) return ""; var d = new Date(v); return isNaN(d.getTime()) ? String(v) : d.toLocaleString(); }
  function cpPaintFoot() {
    if (!cpFoot) return;
    cpFoot.textContent = "";
    var dirty = cpDirty();
    var line = CP_SAVE.busy ? "Saving…"
      : CP_SAVE.error ? "Not saved: " + CP_SAVE.error
      : CP_SAVE.note ? CP_SAVE.note
      : dirty ? "Unsaved — previewed on this page only. Save makes it what everyone using " + APP_NAME + " sees."
      : CP_META && CP_META.updated_at ? "Last changed " + cpWhen(CP_META.updated_at) + (CP_META.updated_by ? " by " + CP_META.updated_by : "") + "."
      : "Every setting is at its default.";
    cpFoot.appendChild(el("span", { class: "ab-savebar-q" + (CP_SAVE.error ? " ab-warn" : ""), role: "status", "aria-live": "polite", text: line }));
    var save = el("button", { class: "ab-primary", type: "button", text: "Save for everyone", disabled: !dirty || CP_SAVE.busy });
    save.addEventListener("click", function (e) { e.stopPropagation(); cpSave(); });
    var discard = el("button", { class: "ab-secondary", type: "button", text: "Discard", disabled: !dirty || CP_SAVE.busy });
    discard.addEventListener("click", function (e) {
      e.stopPropagation(); CP_DRAFT = cpClone(CP); CP_SAVE = { busy: false, note: "", error: "" };
      publishProps(CP, false); cpFill();
    });
    cpFoot.appendChild(save); cpFoot.appendChild(discard);
  }
  function cpFill() {
    cpBox.textContent = "";
    var close = bandBtn("x", "Close");
    close.addEventListener("click", function (e) { e.stopPropagation(); closeOpen(); });
    cpBox.appendChild(el("div", { class: "ab-box-head" }, [el("div", null, [
      el("strong", { text: "Component settings for " + APP_NAME }),
      el("small", { text: "For everyone using " + APP_NAME + ", not just you. Your own look is in Preferences." })]), close]));
    var body = el("div", { class: "ab-box-body" });
    var sections = cpSchema();
    if (CP_ERR && !CP_META) {
      body.appendChild(el("p", { class: "ab-note ab-warn", text: "The hub's settings for this app could not be read: " + CP_ERR + "." }));
    } else if (!CP_META) {
      body.appendChild(el("p", { class: "ab-note", text: "Reading this app's settings…" }));
    } else if (!sections.length) {
      body.appendChild(el("p", { class: "ab-note", text: "No hosted component declares a setting yet." }));
    } else {
      if (!sections.some(function (s) { return s.component === CP_TAB; })) CP_TAB = sections[0].component;
      var tabs = el("div", { class: "ab-cp-tabs", role: "tablist", "aria-label": "Components" });
      sections.forEach(function (s, i) {
        var on = s.component === CP_TAB;
        var t = el("button", { class: "ab-cp-tab" + (on ? " ab-on" : ""), type: "button", role: "tab",
                               "aria-selected": on ? "true" : "false", tabindex: on ? "0" : "-1", text: s.title || s.component });
        t.addEventListener("click", function (e) { e.stopPropagation(); CP_TAB = s.component; cpFill(); });
        t.addEventListener("keydown", function (e) {
          var step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
          if (!step) return;
          e.preventDefault();
          CP_TAB = sections[(i + step + sections.length) % sections.length].component; cpFill();
          var next = cpBox.querySelector(".ab-cp-tab.ab-on"); if (next) next.focus();
        });
        tabs.appendChild(t);
      });
      body.appendChild(tabs);
      var sec = sections.filter(function (s) { return s.component === CP_TAB; })[0];
      var panel = el("div", { class: "ab-cp-panel", role: "tabpanel", "aria-label": sec.title || sec.component });
      list(sec.fields).forEach(function (f) { if (f && f.key) panel.appendChild(cpField(sec.component, f)); });
      body.appendChild(panel);
      var hist = list(CP_META.history).slice().reverse();
      if (hist.length) {
        body.appendChild(el("details", { class: "ab-cp-hist" }, [el("summary", { text: "Recent changes (" + hist.length + ")" })].concat(
          hist.map(function (h) {
            return el("p", { class: "ab-pp-ev" }, [el("small", { text: cpWhen(h.at) }),
              el("span", { text: " " + (h.by || "someone") + " changed " + list(h.changed).join(", ") })]);
          }))));
      }
    }
    cpBox.appendChild(body);
    cpFoot = el("div", { class: "ab-savebar ab-cp-foot", role: "group", "aria-label": "Save component settings" });
    cpBox.appendChild(cpFoot);
    cpPaintFoot();
  }
  function cpSave() {
    if (!CP_DRAFT || CP_SAVE.busy) return;
    CP_SAVE = { busy: true, note: "", error: "" }; cpPaintFoot();
    fetch(CP_SAVE_URL, { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": csrf() },
      body: JSON.stringify({ app: APP, props: CP_DRAFT }) })
      .then(function (r) {
        return r.json().then(function (b) { return { r: r, b: b || {} }; }, function () { return { r: r, b: {} }; });
      })
      .then(function (res) {
        var d = res.b.data;
        if (!res.r.ok || !d) {
          var err = (list(res.b.errors)[0] || {}).msg;
          throw new Error(err || (res.r.status === 404 ? "this page has no route to save component settings (HTTP 404)" : "the hub answered HTTP " + res.r.status));
        }
        CP = d.props || {};
        CP_META = CP_META || {};
        CP_META.props = CP; CP_META.updated_at = d.updated_at; CP_META.updated_by = d.updated_by; CP_META.history = d.history;
        CP_DRAFT = cpClone(CP);
        publishProps(CP, false);
        var refused = list(d.refused);
        CP_SAVE = { busy: false, error: "", note: refused.length
          ? "Saved, except " + refused.join(", ") + " (not an allowed value — the rest is live)."
          : "Saved — everyone using " + APP_NAME + " sees this on their next page." };
        cpFill();
      })
      .catch(function (err) { CP_SAVE = { busy: false, note: "", error: String(err && err.message || err) }; cpPaintFoot(); });
  }

  /* The read-me is a dialog that is HIDDEN until opened -- [hidden] is enforced with
     !important in the stylesheet, because a transparent box that is merely invisible still
     swallows every click and wheel over the page beneath it. */
  var readBox = el("div", { class: "ab-box ab-read ab-surface", role: "dialog", "aria-modal": "true", "aria-label": "About this app", hidden: true });
  function readSection(head, lines) {
    lines = lines.filter(Boolean);
    if (!lines.length) return null;
    return el("section", { class: "ab-read-sec" }, [el("h3", { class: "ab-sec-h", text: head }),
      el("ul", null, lines.map(function (t) { return el("li", { text: t }); }))]);
  }
  function derivedReadme() {
    var frag = doc.createDocumentFragment();
    frag.appendChild(el("p", { class: "ab-read-lead", text: APP_NAME + (D.appSub ? " — " + D.appSub : "") + (D.version ? " · " + vstr(D.version) : "") }));
    [readSection("Who you are here", [
        D.actor ? "You are signed in as " + D.actor + (D.actorSub ? " (" + D.actorSub + ")" : "") + "." : "",
        D.role ? "Your role here is: " + D.role + "." : "",
        D.signout ? "Sign out is at the foot of the drawer behind your icon." : ""]),
     readSection("The pages of this app", NAV.map(function (n) {
        var kids = list(n.children).map(function (c) { return c.label; }).filter(Boolean);
        return n.label ? (kids.length ? n.label + ": " + kids.join(", ") : n.label) + (n.hint ? " — " + n.hint : "") : "";
      })),
     readSection("Key things on this page", pageTour().map(function (s) { return s.title + " — " + s.text; })),
     readSection("What the band gives you", [
        vis(q(".ab-slot:not(.ab-slot--empty)")) ? "This app's own controls, left of the line." : "",
        "Your apps, with stars, from the grid button.",
        "Your preferences, your icon, help and Sign out, behind your icon.",
        "Info bubbles on anything marked, which you can switch off."])
    ].forEach(function (s) { if (s) frag.appendChild(s); });
    frag.appendChild(el("p", { class: "ab-note", text: "Assembled from what is on screen right now, so it cannot describe something that is no longer here. What " + APP_NAME + " is FOR is the app's to write (data-readme-url)." }));
    return frag;
  }
  function readmeFromDoc(d) {
    var frag = doc.createDocumentFragment();
    if (d.lead) frag.appendChild(el("p", { class: "ab-read-lead", text: d.lead }));
    list(d.sections).forEach(function (s) { var sec = readSection(s.head || "", list(s.lines)); if (sec) frag.appendChild(sec); });
    if (d.note) frag.appendChild(el("p", { class: "ab-note", text: d.note }));
    return frag.childNodes.length ? frag : derivedReadme();
  }
  function openReadme(anchor) {
    readBox.textContent = "";
    var close = bandBtn("x", "Close");
    close.addEventListener("click", function (e) { e.stopPropagation(); closeOpen(); });
    readBox.appendChild(el("div", { class: "ab-box-head" }, [el("strong", { text: "About " + APP_NAME }), close]));
    readBox.appendChild(el("div", { class: "ab-box-body" }, [README_DOC ? readmeFromDoc(README_DOC) : derivedReadme()]));
    placeUnderChrome(readBox);
    openAs("readme", anchor, function () { readBox.hidden = true; }, true);
    readBox.hidden = false;
    try { close.focus(); } catch (e) {}
  }
  function fetchJson(url, then) {
    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; }).then(then).catch(function () {});
  }
  if (D.tourUrl) fetchJson(D.tourUrl, function (b) { var s = list(b && (b.steps || b)); TOUR.declared = s.length ? s : null; });
  if (D.readmeUrl) fetchJson(D.readmeUrl, function (b) { if (b) README_DOC = b; });

  /* ----------------------------------------------------------------- mount ----
     Every body-appended surface is created ABOVE this line and appended HERE, once: appending
     a surface before its declaration runs against `undefined` (var hoists the name, not the
     value), throws, and silently kills every line after it at load. */
  [scrim, chip, info, appsPop, drawer, markMenu, fileInput, crop, ppBox, cpBox, tourScrim, tourRing, tourCard, readBox]
    .forEach(function (n) { doc.body.appendChild(n); });
  applyPrefs();
  slotSync();
  buildNav();
  publishChrome();
  loadProfile();
  cpLoad(function () { if (openThing && openThing.kind === "drawer") fillDrawer(); });
  if (accessUrl() && isAdmin(realRole())) accessLoad(function () { if (openThing && openThing.kind === "drawer") fillDrawer(); });
  win.addEventListener("load", function () { slotSync(); publishChrome(); });
  mount.setAttribute("data-ab-ready", "1");
  try { doc.dispatchEvent(new CustomEvent("app-banner:ready", { detail: { app: APP } })); } catch (e) {}
})();
