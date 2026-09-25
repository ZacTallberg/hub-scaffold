/* =============================================================================
   CONTEXT MENU — one right-click menu for every app around the hub.  v1.0.1

   A hosted component: an app LINKS these files from the hub and never copies
   them, so a change here reaches every adopting app on its next page load with no
   app deploy.

   WHAT IT IS. The APP declares what can be right-clicked on its pages and what can
   be done to each thing -- a JSON catalog island inside the mount -- and this file
   does everything a menu does: the pointer, the keyboard (Shift+F10, the Menu
   key, arrows, type-ahead), touch long-press, tabs, submenus, a check on the
   current value, in-place confirmation for anything destructive, the writes, the
   toasts, each person's recently-used row, and the optional smart pass. It knows
   nothing about any one app's records.

   THE HUB SERVES THIS COMPONENT AND NEVER AN APP'S DATA. Every URL an item carries
   is a path on the app's own origin behind the app's own sign-in; the component
   POSTs to it with the app's own CSRF token exactly as the app's own forms do. It
   never invents an action: it can only offer what the catalog declared.

   THE SMART PASS. With data-smart-url set, the component asks the app's bridge --
   which asks the hub (POST /hub/api/menu/rank), which asks the model the operator
   configured -- how to ORGANISE the declared items for this app, page, kind of
   thing and role: which to pin at the top, how to split them into tabs, which
   optional ones to drop, and up to three questions worth asking the page's agent
   about the thing under the pointer. The answer can only name ids the catalog
   declared, and the hub checks that before this file sees it. Until it lands the
   menu is the catalog's own order, so the smart pass never makes anyone wait; the
   answer is remembered for the session, so a page asks once per kind, not once
   per click. A model that is down, slow or wrong costs nothing: the failure mode of
   a heuristic is "do nothing".

   THE MOUNT
       <div id="hub-menu" hidden data-app="<slug>" data-app-name="..." data-page="<key>"
            data-role="<rung>" data-smart-url="<the app's gated bridge, optional>"
            data-props="/hub/components/props/<slug>.json">
         <script type="application/json" data-menu="catalog">{...}</script>
       </div>
       <link rel="stylesheet" href="/hub/components/context-menu/context-menu.css">
       <script src="/hub/components/context-menu/context-menu.js"></script>
   The script is NOT deferred and sits after the mount. The full catalog contract
   is in manifest.json beside this file.

   EVENTS an app may listen for (all on document):
       hub-menu:ready   { version, kinds }
       hub-menu:open    { kind, id, target, facts, items }  cancelable; detail.add(item)
       hub-menu:action  { kind, id, item, target, facts, action } before any action runs
       hub-menu:done    { kind, id, item, status, body }    after a post/htmx landed
   It dispatches hub:agent-ask (cancelable, { question }) for an "ask" action; the
   agent component cancels it once it has taken the question.
   And a global, for an app that wants to open it from its own control:
       window.HubMenu = { open(target, x, y), close(), register(kind, def), kinds(), smart(), version }
   ============================================================================= */
(function () {
  "use strict";
  var VERSION = "1.0.1";
  var doc = document;
  var mount = doc.getElementById("hub-menu") || doc.querySelector("[data-hub-menu]");
  if (!mount || mount.getAttribute("data-hcm-ready") === "1") return;
  mount.setAttribute("data-hcm-ready", "1");
  mount.setAttribute("data-hcm-version", VERSION);

  var APP = mount.getAttribute("data-app") || "";
  var APP_NAME = mount.getAttribute("data-app-name") || APP;
  var PAGE = mount.getAttribute("data-page") || (location.pathname.replace(/\/+$/, "") || "/");
  var SMART_URL = mount.getAttribute("data-smart-url") || "";
  var ROLE = mount.getAttribute("data-role") || "";
  var ID = "hub-menu-open";

  /* ------------------------------------------------------------ properties */
  // An operator sets these per app (POST /hub/api/component-props); they arrive as
  // window.HubComponentProps, from data-props, or on the hub:component-props event.
  // Defaults match this component's manifest "props".
  var PROPS = { smart: "on", tabs: "auto", recent: "on", head: "on", facts: "on" };
  function takeProps(all) {
    var p = all && (all["context-menu"] || (all.props || {})["context-menu"]);
    if (!p) return;
    ["smart", "tabs", "recent", "head", "facts"].forEach(function (k) {
      if (typeof p[k] === "string" && p[k]) PROPS[k] = p[k];
    });
  }
  try { if (window.HubComponentProps) takeProps(window.HubComponentProps); } catch (e) {}
  window.addEventListener("hub:component-props", function (e) {
    var det = (e && e.detail) || {};
    if (det.app && det.app !== APP) return;
    takeProps(det.props || det);
  });
  (function () {
    var url = mount.getAttribute("data-props");
    if (!url) return;
    fetch(url + (url.indexOf("?") >= 0 ? "&" : "?") + "component=context-menu",
          { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (b) { if (b && b.props) takeProps(b.props); })
      .catch(function () { /* the defaults stand */ });
  })();

  /* --------------------------------------------------------------- catalog */
  function readIsland(kind) {
    var node = mount.querySelector('script[type="application/json"][data-menu="' + kind + '"]');
    if (!node) return null;
    try { return JSON.parse(node.textContent || "null"); } catch (e) { fail(e); return null; }
  }
  var CATALOG = readIsland("catalog") || {};
  var KINDS = CATALOG.kinds || {};

  // An error in this file must reach the app's error visibility, never be swallowed:
  // rethrown off the stack so window.onerror (and the app's error forwarder) sees it, while
  // the gesture that caused it still gets the browser's own menu rather than nothing.
  function fail(err) {
    try { console.error("[hub-menu]", err); } catch (e) {}
    setTimeout(function () { throw err; }, 0);
  }

  /* ----------------------------------------------------------------- utils */
  function el(tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function ssGet(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }
  function ssSet(k, v) { try { sessionStorage.setItem(k, v); } catch (e) {} }

  // CSRF, and never by Django's default cookie name (every app here namespaces it
  // by slug). A token the page puts on <body data-csrf> comes first.
  function csrfToken() {
    var fromBody = (doc.body && doc.body.dataset && doc.body.dataset.csrf) || "";
    if (fromBody) return fromBody;
    var input = doc.querySelector("input[name=csrfmiddlewaretoken]");
    if (input && input.value) return input.value;
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

  // Stroke-only glyphs, inline, so the menu owes the app no icon font. An app that has
  // an icon font may still say icon: "ph:<class names>" and get its own glyph.
  var ICONS = {
    open: '<path d="M14 3h-4M14 3v4M14 3l-6 6"/><path d="M12 9v3.5a1.5 1.5 0 0 1-1.5 1.5h-7A1.5 1.5 0 0 1 2 12.5v-7A1.5 1.5 0 0 1 3.5 4H7"/>',
    tab: '<rect x="2" y="3" width="12" height="10" rx="1.5"/><path d="M2 6h12"/>',
    link: '<path d="M6.5 9.5a3 3 0 0 0 4.2 0l2-2a3 3 0 0 0-4.2-4.2l-1 1"/><path d="M9.5 6.5a3 3 0 0 0-4.2 0l-2 2a3 3 0 0 0 4.2 4.2l1-1"/>',
    hash: '<path d="M6 2 4.5 14M11.5 2 10 14M2.5 6h11M2 10h11"/>',
    copy: '<rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 5.5V3.5A1.5 1.5 0 0 0 9 2H3.5A1.5 1.5 0 0 0 2 3.5V9a1.5 1.5 0 0 0 1.5 1.5h2"/>',
    columns: '<rect x="2" y="2.5" width="12" height="11" rx="1.5"/><path d="M6 2.5v11M10 2.5v11"/>',
    flag: '<path d="M3.5 14V2.5"/><path d="M3.5 3h8l-2 3 2 3h-8"/>',
    heart: '<path d="M2 8h3l1.5-3 2 6 1.5-3H14"/>',
    user: '<circle cx="8" cy="5.5" r="2.75"/><path d="M2.5 14a5.5 5.5 0 0 1 11 0"/>',
    users: '<circle cx="6" cy="5.5" r="2.5"/><path d="M1.5 13.5a4.5 4.5 0 0 1 9 0"/><path d="M10.5 3.5a2.5 2.5 0 0 1 0 4.5M12 9.5a4.5 4.5 0 0 1 2.5 4"/>',
    star: '<path d="m8 2 1.8 3.8 4.2.6-3 2.9.7 4.2L8 11.5l-3.7 2 .7-4.2-3-2.9 4.2-.6z"/>',
    calendar: '<rect x="2" y="3" width="12" height="11" rx="1.5"/><path d="M2 7h12M5.5 1.5V4M10.5 1.5V4"/>',
    clock: '<circle cx="8" cy="8" r="6"/><path d="M8 4.5V8l2.5 1.5"/>',
    trash: '<path d="M2.5 4h11M6 4V2.5h4V4M4 4l.7 9.5h6.6L12 4"/>',
    sparkle: '<path d="M8 2v3M8 11v3M2 8h3M11 8h3M4.2 4.2l2 2M9.8 9.8l2 2M11.8 4.2l-2 2M6.2 9.8l-2 2"/>',
    question: '<circle cx="8" cy="8" r="6"/><path d="M6.2 6.3a1.9 1.9 0 1 1 2.6 1.8c-.6.3-.8.7-.8 1.3"/><path d="M8 11.6v.1"/>',
    refresh: '<path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9"/><path d="M13.5 2.5v3h-3"/>',
    plus: '<path d="M8 3v10M3 8h10"/>',
    eye: '<path d="M1.5 8s2.5-4.5 6.5-4.5S14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z"/><circle cx="8" cy="8" r="2"/>',
    download: '<path d="M8 2.5v8M4.5 7 8 10.5 11.5 7"/><path d="M2.5 13.5h11"/>',
    list: '<path d="M5 4h9M5 8h9M5 12h9M2 4h.01M2 8h.01M2 12h.01"/>',
    history: '<path d="M2.5 8a5.5 5.5 0 1 0 1.6-3.9"/><path d="M2.5 2.5v3h3"/><path d="M8 5v3l2 1.5"/>',
    chat: '<path d="M2.5 3.5h11v7h-6L4 13.5v-3H2.5z"/>',
    check: '<path d="m3 8.5 3 3 7-7"/>',
    caret: '<path d="m6 3.5 4.5 4.5L6 12.5"/>',
    down: '<path d="m3.5 6 4.5 4.5L12.5 6"/>',
    pin: '<path d="M9.5 2 14 6.5l-2 1-1.5 4L8 9 4 13 3 12l4-4-2.5-2.5 4-1.5z"/>',
    search: '<circle cx="7" cy="7" r="4.5"/><path d="m10.5 10.5 3.5 3.5"/>',
    dot: '<circle cx="8" cy="8" r="3.5" fill="currentColor" stroke="none"/>',
    circle: '<circle cx="8" cy="8" r="5"/>',
    play: '<path d="M4 2.5v11l9-5.5z"/>',
    edit: '<path d="M11 2.5 13.5 5 6 12.5H3.5V10z"/><path d="M9.5 4l2.5 2.5"/>',
    info: '<circle cx="8" cy="8" r="6"/><path d="M8 7v4M8 5v.1"/>',
    mail: '<rect x="2" y="3.5" width="12" height="9" rx="1.5"/><path d="m2 5 6 4 6-4"/>',
    board: '<rect x="2" y="2.5" width="12" height="11" rx="1.5"/><path d="M6 2.5v11M10 2.5v6"/>',
    ship: '<path d="M2 11.5c1.5 1.2 3 1.2 4.5 0 1.5 1.2 3 1.2 4.5 0 1 .8 2 1 3 .5"/><path d="M3 10.5 2.5 8h11L13 10.5"/><path d="M4.5 8V4.5h7V8M8 2.5v2"/>',
    grid: '<rect x="2" y="2" width="5" height="5" rx="1"/><rect x="9" y="2" width="5" height="5" rx="1"/><rect x="2" y="9" width="5" height="5" rx="1"/><rect x="9" y="9" width="5" height="5" rx="1"/>',
    x: '<path d="M4 4l8 8M12 4l-8 8"/>'
  };
  function icon(name, color) {
    var wrap = el("span", "hcm-ico");
    wrap.setAttribute("aria-hidden", "true");
    if (!name) return wrap;
    if (name.indexOf("ph:") === 0) {
      var i = el("i", "ph " + name.slice(3));
      if (color) i.style.color = color;
      wrap.appendChild(i);
      return wrap;
    }
    if (name === "dot" && color) {
      var d = el("span", "hcm-dot");
      d.style.color = color;
      wrap.appendChild(d);
      return wrap;
    }
    var path = ICONS[name];
    if (!path) path = ICONS.circle;
    wrap.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true">' + path + "</svg>";
    if (color) wrap.style.color = color;
    return wrap;
  }

  /* -------------------------------------------------------- toast (tiny) */
  var toastNode = null, toastTimer = null;
  function toast(text, tone) {
    if (toastNode) toastNode.remove();
    toastNode = el("div", "hcm-toast" + (tone === "error" ? " is-error" : ""), text);
    toastNode.setAttribute("role", "status");
    doc.body.appendChild(toastNode);
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { if (toastNode) { toastNode.remove(); toastNode = null; } },
                            tone === "error" ? 6000 : 2400);
  }

  /* ------------------------------------------------------------ templating */
  // `{name}` is a fact of the target; `{id}`, `{href}`, `{text}`, `{page}`, `{origin}`
  // are always there. Unknown names become "" so a URL never carries a literal brace.
  function fill(tpl, facts) {
    if (typeof tpl !== "string") return tpl;
    return tpl.replace(/\{([a-zA-Z0-9_\-:.]+)\}/g, function (_, k) {
      var v = facts[k];
      return v == null ? "" : String(v);
    });
  }
  function fillDeep(v, facts) {
    if (typeof v === "string") return fill(v, facts);
    if (Array.isArray(v)) return v.map(function (x) { return fillDeep(x, facts); });
    if (v && typeof v === "object") {
      var out = {};
      Object.keys(v).forEach(function (k) { out[k] = fillDeep(v[k], facts); });
      return out;
    }
    return v;
  }
  // A condition is an object: {"status": "3"} equal, {"!status": "3"} not equal,
  // {"owner": "*"} present and non-empty, {"owner": ""} absent. All clauses must hold.
  function holds(cond, facts) {
    if (!cond) return true;
    if (typeof cond === "boolean") return cond;
    if (typeof cond !== "object") return true;
    return Object.keys(cond).every(function (k) {
      var want = fill(String(cond[k] == null ? "" : cond[k]), facts);
      var neg = k.charAt(0) === "!";
      var key = neg ? k.slice(1) : k;
      var have = facts[key] == null ? "" : String(facts[key]);
      var ok;
      if (want === "*") ok = have !== "";
      else ok = have === want;
      return neg ? !ok : ok;
    });
  }

  /* --------------------------------------------------------------- targets */
  // A kind's targets: its own `match` selector, else [data-hcm="<kind>"]. The id is the
  // first of its `id` attribute names that is set (default data-hcm-id), and every
  // data-* attribute of the target becomes a fact, keyed as written after "data-".
  function kindSelector(kind, def) {
    return def.match || ('[data-hcm="' + kind + '"]');
  }
  function allSelector() {
    var parts = [];
    Object.keys(KINDS).forEach(function (k) {
      if (k === "page") return;
      parts.push(kindSelector(k, KINDS[k]));
    });
    return parts.join(", ");
  }
  function findTarget(from) {
    var sel = allSelector();
    if (!sel) return null;
    var node = from && from.closest ? from.closest(sel) : null;
    if (!node) return null;
    var kinds = Object.keys(KINDS);
    for (var i = 0; i < kinds.length; i++) {
      var k = kinds[i];
      if (k === "page") continue;
      try { if (node.matches(kindSelector(k, KINDS[k]))) return { kind: k, node: node }; } catch (e) {}
    }
    return null;
  }
  function factsOf(kind, node) {
    var def = KINDS[kind] || {};
    var facts = {};
    if (node && node.attributes) {
      for (var i = 0; i < node.attributes.length; i++) {
        var a = node.attributes[i];
        if (a.name.indexOf("data-") === 0) facts[a.name.slice(5)] = a.value;
      }
    }
    var idAttrs = (def.id || "hcm-id").split(/\s+/);
    var id = "";
    for (var j = 0; j < idAttrs.length && !id; j++) id = facts[idAttrs[j]] || "";
    facts.id = id;
    var anchor = node ? node.querySelector("a[href]") : null;
    facts.href = facts.href || (anchor ? anchor.getAttribute("href") : "");
    facts["href-abs"] = facts.href ? new URL(facts.href, location.href).href : "";
    var labelNode = node ? (node.querySelector("[data-hcm-label]") || node.querySelector("h1,h2,h3,h4,a,th,strong")) : null;
    facts.text = facts["hcm-label"] || (labelNode ? (labelNode.textContent || "").trim().slice(0, 80) : "");
    facts.page = location.href;
    facts.origin = location.origin;
    facts.kind = kind;
    return facts;
  }

  /* ----------------------------------------------------------- recent use */
  // Decayed counts per app + kind, this browser only: what THIS person reaches for.
  function recentKey(kind) { return "hcm:" + APP + ":" + kind; }
  function readRecent(kind) {
    var raw = lsGet(recentKey(kind));
    var data = {};
    try { data = raw ? JSON.parse(raw) : {}; } catch (e) { data = {}; }
    var now = Date.now(), out = {};
    Object.keys(data).forEach(function (id) {
      var r = data[id];
      if (!r || typeof r.n !== "number") return;
      var days = (now - (r.t || now)) / 864e5;
      var n = r.n * Math.pow(0.93, days);
      if (n >= 0.5) out[id] = { n: n, t: r.t || now };
    });
    return out;
  }
  function bump(kind, id) {
    var data = readRecent(kind);
    var r = data[id] || { n: 0, t: Date.now() };
    r.n = r.n + 1; r.t = Date.now();
    data[id] = r;
    lsSet(recentKey(kind), JSON.stringify(data));
  }
  function topRecent(kind, n) {
    var data = readRecent(kind);
    return Object.keys(data)
      .map(function (id) { return { id: id, n: data[id].n }; })
      .filter(function (r) { return r.n >= 3; })
      .sort(function (a, b) { return b.n - a.n; })
      .slice(0, n);
  }

  /* -------------------------------------------------------------- smart pass */
  // One answer per (app, page, kind) per session. Asked when the page first shows a
  // kind, so the first right-click is usually already organised; asked again on the
  // first open if that prefetch has not landed. Never blocks: the menu draws first.
  var SMART = {};          // kind -> {state: "idle"|"busy"|"done"|"off", answer, at}
  function smartOn() { return !!SMART_URL && PROPS.smart !== "off"; }
  function smartKey(kind) { return "hcm-smart:" + APP + ":" + PAGE + ":" + kind + ":" + itemSignature(kind); }
  function itemSignature(kind) {
    var def = KINDS[kind] || {};
    return (def.items || []).map(function (it) { return it.id || ""; }).join(",").length + ":" +
           (def.items || []).length;
  }
  function flatItems(items, out) {
    out = out || [];
    (items || []).forEach(function (it) {
      if (!it || it.sep || !it.id) return;
      out.push({ id: it.id, label: it.label || it.id, hint: it.hint || "", group: it.group || "",
                 sub: !!(it.sub && it.sub.length), maybe: !!it.maybe, danger: !!it.danger });
    });
    return out;
  }
  function smartFetch(kind, facts) {
    if (!smartOn() || !KINDS[kind]) return;
    var s = SMART[kind] || (SMART[kind] = { state: "idle" });
    if (s.state === "busy" || s.state === "done") return;
    var cached = ssGet(smartKey(kind));
    if (cached) {
      try {
        var c = JSON.parse(cached);
        if (c && c.at && Date.now() - c.at < 30 * 60 * 1000) { s.state = "done"; s.answer = c.answer; return; }
      } catch (e) {}
    }
    s.state = "busy";
    var def = KINDS[kind];
    // Only the KEYS of the target's facts and short, non-identifying values ride along:
    // enough for the model to know what kind of thing this is, never a record's body.
    var sample = {};
    Object.keys(facts || {}).forEach(function (k) {
      if (["page", "origin", "href", "href-abs", "text"].indexOf(k) >= 0) return;
      var v = String(facts[k] == null ? "" : facts[k]);
      if (v.length <= 40) sample[k] = v;
    });
    var body = {
      app: APP, app_name: APP_NAME, page: PAGE, kind: kind,
      kind_label: def.label || kind, role: ROLE,
      items: flatItems(def.items), facts: sample,
      recent: topRecent(kind, 5), has_agent: !!doc.querySelector("[data-hub-agent]")
    };
    var started = Date.now();
    fetch(SMART_URL, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken(),
                 "X-Requested-With": "XMLHttpRequest", "Accept": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json().then(function (d) { return { status: r.status, data: d }; }); })
      .then(function (res) {
        var d = res.data || {};
        if (d.ok && d.ranking) {
          s.state = "done"; s.answer = d.ranking; s.ms = Date.now() - started;
          ssSet(smartKey(kind), JSON.stringify({ at: Date.now(), answer: d.ranking }));
          mount.setAttribute("data-hcm-smart", "on");
          // A menu still open for this kind takes the answer if it came quickly enough
          // that nobody has started reading yet; otherwise the next open takes it.
          if (openState && openState.kind === kind && Date.now() - openState.at < 450) {
            var o = openState; close(); build(o.kind, o.node, o.x, o.y, o.viaKeyboard);
          } else if (openState && openState.kind === kind) {
            var star = openState.menu.querySelector(".hcm-star");
            if (star) { star.removeAttribute("data-busy"); star.title = "Agent powered: organised for the next open"; }
          }
        } else {
          s.state = "off"; s.reason = d.reason || ("http " + res.status); s.error = d.error || "";
          mount.setAttribute("data-hcm-smart", s.reason === "unconfigured" ? "unconfigured" : "failed");
          if (openState && openState.kind === kind) {
            var st = openState.menu.querySelector(".hcm-star");
            if (st) st.remove();
          }
        }
      })
      .catch(function (err) {
        s.state = "off"; s.reason = "unreachable"; s.error = String(err && err.message || err);
        mount.setAttribute("data-hcm-smart", "failed");
      });
  }
  function prefetch() {
    if (!smartOn()) return;
    Object.keys(KINDS).forEach(function (kind) {
      if (kind === "page") return;
      var node = doc.querySelector(kindSelector(kind, KINDS[kind]));
      if (node) smartFetch(kind, factsOf(kind, node));
    });
  }

  /* ------------------------------------------------------------------ build */
  var openState = null;   // {kind, id, node, facts, menu, x, y, at, viaKeyboard}
  var confirmArmed = null;

  function itemsFor(kind, facts, node) {
    var def = KINDS[kind] || {};
    var items = (def.items || []).map(function (it) { return it; });
    // The app may add to the list for THIS target (hub-menu:open detail.add) or cancel.
    var added = [];
    var ev;
    try {
      ev = new CustomEvent("hub-menu:open", { cancelable: true, detail: {
        kind: kind, id: facts.id, target: node, facts: facts, items: items,
        add: function (it) { if (it) added.push(it); }
      } });
      if (!doc.dispatchEvent(ev)) return null;
    } catch (e) { fail(e); }
    return items.concat(added);
  }

  function build(kind, node, x, y, viaKeyboard) {
    close();
    var def = KINDS[kind];
    if (!def) return false;
    var facts = factsOf(kind, node);
    var items = itemsFor(kind, facts, node);
    if (items === null) return false;
    var visible = items.filter(function (it) { return it && (it.sep || holds(it.when, facts)); });
    // Sub items obey their own `when`; a submenu with nothing left in it is dropped.
    visible = visible.map(function (it) {
      if (!it.sub) return it;
      var sub = it.sub.filter(function (s) { return s && (s.sep || holds(s.when, facts)); });
      if (!sub.some(function (s) { return !s.sep; })) return null;
      var copy = {}; Object.keys(it).forEach(function (k) { copy[k] = it[k]; }); copy.sub = sub;
      return copy;
    }).filter(Boolean);
    if (!visible.some(function (it) { return !it.sep; })) return false;

    var menu = el("div", "hcm");
    menu.id = ID;
    menu.setAttribute("role", "menu");
    menu.setAttribute("data-kind", kind);
    var headId = fill(def.head || "{id}", facts);
    menu.setAttribute("aria-label", (def.label || kind) + " " + headId);
    if (def.width) menu.style.minWidth = def.width;

    var smart = SMART[kind];
    var ranking = smart && smart.state === "done" ? smart.answer : null;

    // HEAD: what this is, and the star when the model organised it.
    if (PROPS.head !== "off") {
      var head = el("div", "hcm-head");
      if (def.label) head.appendChild(el("span", "hcm-head-kind", def.label));
      head.appendChild(el("span", "hcm-head-id", headId));
      if (smartOn() && (!smart || smart.state !== "off")) {
        var star = el("span", "hcm-star");
        star.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true">' + ICONS.sparkle + "</svg>";
        star.title = ranking ? "Organised by the hub's model for this app"
                             : "Asking the hub's model how to organise this menu…";
        if (!ranking) star.setAttribute("data-busy", "1");
        head.appendChild(star);
      }
      menu.appendChild(head);
    }
    // FACTS: the values behind the state, when the catalog names which to show.
    if (PROPS.facts !== "off" && def.facts && def.facts.length) {
      var fx = el("div", "hcm-facts");
      def.facts.forEach(function (f) {
        var label = typeof f === "string" ? f : f.label;
        var key = typeof f === "string" ? f : f.key;
        var val = fill("{" + key + "}", facts);
        if (!val) return;
        var s = el("span");
        s.appendChild(el("b", null, label + " "));
        s.appendChild(doc.createTextNode(typeof f === "object" && f.map && f.map[val] ? f.map[val] : val));
        fx.appendChild(s);
      });
      if (fx.childNodes.length) menu.appendChild(fx);
    }

    var byId = {};
    visible.forEach(function (it) { if (it.id) byId[it.id] = it; });

    // RECENT: this person's own two most-used, when they have used them enough to mean it.
    var recentRow = null;
    if (PROPS.recent !== "off") {
      var rec = topRecent(kind, 2).filter(function (r) { return byId[r.id] && !byId[r.id].sub; });
      if (rec.length) {
        recentRow = doc.createDocumentFragment();
        recentRow.appendChild(el("div", "hcm-label", "Recent"));
        rec.forEach(function (r) { recentRow.appendChild(itemNode(byId[r.id], kind, facts, node, menu)); });
        recentRow.appendChild(el("div", "hcm-sep"));
      }
    }

    // PINNED (model): up to three moved to the top, under the recent row.
    var pinned = [];
    if (ranking && ranking.pinned && ranking.pinned.length) {
      ranking.pinned.forEach(function (id) { if (byId[id] && pinned.indexOf(id) < 0) pinned.push(id); });
    }
    var hidden = {};
    if (ranking && ranking.hidden) {
      ranking.hidden.forEach(function (id) { if (byId[id] && byId[id].maybe) hidden[id] = true; });
    }

    // ASKS (model): questions for the page's agent about this thing -- only
    // where the page has the agent to ask.
    var asks = (ranking && ranking.asks || []).filter(function (a) { return a && a.question; }).slice(0, 3);
    if (asks.length && doc.querySelector("[data-hub-agent]")) {
      visible.push({ sep: true });
      visible.push({ id: "__ask", icon: "chat", label: "Ask the agent", sub: asks.map(function (a, i) {
        return { id: "__ask" + i, icon: "question", label: a.label || a.question,
                 do: { type: "ask", question: a.question } };
      }) });
    }

    // TABS: the catalog's own, else the model's, else none. A pinned or hidden item is
    // still placed by its tab so nothing is lost, only re-ordered.
    var tabs = null;
    if (PROPS.tabs !== "off") {
      if (def.tabs && def.tabs.length > 1) tabs = def.tabs;
      else if (ranking && ranking.tabs && ranking.tabs.length > 1) tabs = ranking.tabs;
    }

    var body = el("div", "hcm-body");
    var placed = {};
    function renderList(container, list) {
      var lastSep = true;
      list.forEach(function (it) {
        if (it.sep) { if (!lastSep) { container.appendChild(el("div", "hcm-sep")); lastSep = true; } return; }
        if (hidden[it.id] || placed[it.id]) return;
        if (it.id) placed[it.id] = true;
        container.appendChild(itemNode(it, kind, facts, node, menu));
        lastSep = false;
      });
      var tail = container.lastElementChild;
      if (tail && tail.classList.contains("hcm-sep")) tail.remove();
    }
    if (recentRow) body.appendChild(recentRow);
    if (pinned.length) {
      var pinFrag = el("div");
      pinned.forEach(function (id) { placed[id] = true; pinFrag.appendChild(itemNode(byId[id], kind, facts, node, menu)); });
      pinFrag.appendChild(el("div", "hcm-sep"));
      body.appendChild(pinFrag);
      // Pinned items are shown once: mark them so tab panes skip them.
    }
    if (tabs) {
      var strip = el("div", "hcm-tabs");
      strip.setAttribute("role", "tablist");
      var panes = [];
      var used = {};
      tabs.forEach(function (t, i) {
        var ids = (t.items || []).filter(function (id) { return byId[id] && !used[id]; });
        ids.forEach(function (id) { used[id] = true; });
        if (!ids.length) return;
        var pane = el("div", "hcm-pane");
        pane.setAttribute("role", "tabpanel");
        var list = [];
        // Keep the catalog's separators where they fall inside the tab's items.
        visible.forEach(function (it) {
          if (it.sep) { list.push(it); return; }
          if (ids.indexOf(it.id) >= 0) list.push(it);
        });
        renderList(pane, list);
        if (!pane.childNodes.length) return;
        var tab = el("button", "hcm-tab", String(t.label || ("Tab " + (i + 1))).slice(0, 20));
        tab.type = "button"; tab.setAttribute("role", "tab");
        panes.push({ tab: tab, pane: pane });
        strip.appendChild(tab);
      });
      // Anything no tab named goes to a trailing More.
      var rest = visible.filter(function (it) { return it.sep || (it.id && !used[it.id] && !placed[it.id] && !hidden[it.id]); });
      if (rest.some(function (it) { return !it.sep; })) {
        var morePane = el("div", "hcm-pane"); morePane.setAttribute("role", "tabpanel");
        renderList(morePane, rest);
        if (morePane.childNodes.length) {
          var moreTab = el("button", "hcm-tab", "More"); moreTab.type = "button"; moreTab.setAttribute("role", "tab");
          panes.push({ tab: moreTab, pane: morePane }); strip.appendChild(moreTab);
        }
      }
      if (panes.length > 1) {
        var tabKey = "hcm-tab:" + APP + ":" + kind;
        var remembered = ssGet(tabKey);
        var current = 0;
        panes.forEach(function (p, i) { if (p.tab.textContent === remembered) current = i; });
        function select(i, focus) {
          panes.forEach(function (p, j) {
            p.tab.setAttribute("aria-selected", i === j ? "true" : "false");
            p.tab.tabIndex = i === j ? 0 : -1;
            p.pane.hidden = i !== j;
          });
          ssSet(tabKey, panes[i].tab.textContent);
          if (focus) { var f = panes[i].pane.querySelector(".hcm-item"); if (f) f.focus(); }
        }
        panes.forEach(function (p, i) {
          p.tab.addEventListener("click", function (e) { e.stopPropagation(); select(i, true); });
          p.tab.addEventListener("keydown", function (e) {
            if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
              e.preventDefault(); e.stopPropagation();
              var n = (i + (e.key === "ArrowRight" ? 1 : -1) + panes.length) % panes.length;
              select(n, false); panes[n].tab.focus();
            }
          });
        });
        menu.appendChild(strip);
        panes.forEach(function (p) { body.appendChild(p.pane); });
        select(current, false);
        menu._hcmTabs = { panes: panes, select: select };
      } else {
        panes.forEach(function (p) { body.appendChild(p.pane); });
      }
    } else {
      renderList(body, visible);
    }
    menu.appendChild(body);
    if (ranking && ranking.note && PROPS.head !== "off") menu.appendChild(el("div", "hcm-note", String(ranking.note).slice(0, 140)));

    doc.body.appendChild(menu);
    place(menu, x, y, node, viaKeyboard);
    var rect0 = node && node.getBoundingClientRect ? node.getBoundingClientRect() : null;
    openState = { kind: kind, id: facts.id, node: node, facts: facts, menu: menu, x: x, y: y,
                  at: Date.now(), viaKeyboard: !!viaKeyboard,
                  top: rect0 ? rect0.top : 0, left: rect0 ? rect0.left : 0 };
    confirmArmed = null;
    var first = menu.querySelector(".hcm-tab[aria-selected='true']") && viaKeyboard
      ? menu.querySelector(".hcm-pane:not([hidden]) .hcm-item") : menu.querySelector(".hcm-pane:not([hidden]) .hcm-item, .hcm-item");
    if (first) first.focus({ preventScroll: true });
    // A page may move focus to its own row on the same gesture (measured on a
    // register page: its mouseup handler focused the row, the menu lost the keyboard). Take
    // it back once, after the page's own handlers have run.
    setTimeout(function () {
      if (openState && openState.menu === menu && first && !menu.contains(doc.activeElement)) {
        try { first.focus({ preventScroll: true }); } catch (e) {}
      }
    }, 0);
    if (smartOn() && (!smart || smart.state === "idle")) smartFetch(kind, facts);
    return true;
  }

  function place(menu, x, y, node, viaKeyboard) {
    if (viaKeyboard && node) {
      var r0 = node.getBoundingClientRect();
      x = r0.left + Math.min(24, r0.width / 2); y = r0.top + Math.min(r0.height, 28);
    }
    var r = menu.getBoundingClientRect();
    var left = Math.min(x, window.innerWidth - r.width - 8);
    var top = Math.min(y, window.innerHeight - r.height - 8);
    menu.style.left = Math.max(8, left) + "px";
    menu.style.top = Math.max(8, top) + "px";
    if (left + r.width + 220 > window.innerWidth) menu.classList.add("flip-subs");
  }

  function itemNode(it, kind, facts, node, menu) {
    var b = el("button", "hcm-item");
    b.type = "button";
    b.setAttribute("role", "menuitem");
    if (it.id) b.setAttribute("data-id", it.id);
    var on = it.on ? holds(it.on, facts) : false;
    if (on) b.classList.add("is-on");
    if (it.danger) b.classList.add("is-danger");
    var color = it.color ? fill(it.color, facts) : "";
    b.appendChild(icon(it.icon || (it.sub ? "list" : ""), color));
    var label = el("span", null, fill(it.label || it.id || "", facts));
    if (it.hint) label.appendChild(el("small", "hcm-hint", fill(it.hint, facts)));
    b.appendChild(label);
    if (it.kbd) b.appendChild(el("kbd", "hcm-kbd", it.kbd));
    if (on) { var c = el("span", "hcm-check"); c.innerHTML = '<svg viewBox="0 0 16 16">' + ICONS.check + "</svg>"; b.appendChild(c); }
    if (it.disabled) { b.disabled = true; if (typeof it.disabled === "string") b.title = fill(it.disabled, facts); }

    if (it.sub && it.sub.length) {
      var wrap = el("div", "hcm-sub");
      b.classList.add("has-sub");
      b.setAttribute("aria-haspopup", "menu");
      var caret = el("span", "hcm-caret"); caret.innerHTML = '<svg viewBox="0 0 16 16">' + ICONS.caret + "</svg>";
      b.appendChild(caret);
      var panel = el("div", "hcm hcm-panel" + (it.sub.length > 9 ? " hcm-scroll" : ""));
      panel.setAttribute("role", "menu");
      var lastSep = true;
      it.sub.forEach(function (s) {
        if (s.sep) { if (!lastSep) { panel.appendChild(el("div", "hcm-sep")); lastSep = true; } return; }
        panel.appendChild(itemNode(s, kind, facts, node, menu)); lastSep = false;
      });
      b.addEventListener("click", function (ev) {
        ev.stopPropagation();
        menu.querySelectorAll(".hcm-sub.is-open").forEach(function (o) { if (o !== wrap) o.classList.remove("is-open"); });
        wrap.classList.toggle("is-open");
        var f = panel.querySelector(".hcm-item");
        if (wrap.classList.contains("is-open") && f) f.focus();
      });
      b.addEventListener("keydown", function (ev) { if (ev.key === "ArrowRight") { ev.preventDefault(); ev.stopPropagation(); b.click(); } });
      panel.addEventListener("keydown", function (ev) {
        if (ev.key === "ArrowLeft") { ev.preventDefault(); ev.stopPropagation(); wrap.classList.remove("is-open"); b.focus(); }
      });
      wrap.appendChild(b); wrap.appendChild(panel);
      return wrap;
    }

    b.addEventListener("click", function (ev) {
      ev.stopPropagation();
      if (b.disabled) return;
      if (it.confirm) {
        // Nothing destructive fires on one press. The first press arms this item and
        // says so; the second, on the same open, runs it. Any other press disarms.
        if (confirmArmed !== b) {
          menu.querySelectorAll(".hcm-item.is-confirm").forEach(function (n) { n.classList.remove("is-confirm"); });
          confirmArmed = b; b.classList.add("is-confirm");
          b.setAttribute("aria-label", fill(it.confirm, facts) + " Press again to confirm.");
          return;
        }
      }
      var o = openState;
      close();
      if (it.id && it.id.indexOf("__") !== 0) bump(kind, it.id);
      run(it, kind, facts, node, o);
    });
    return b;
  }

  /* ---------------------------------------------------------------- actions */
  function run(it, kind, facts, node, o) {
    var d = it.do ? fillDeep(it.do, facts) : null;
    try {
      doc.dispatchEvent(new CustomEvent("hub-menu:action", { detail: {
        kind: kind, id: facts.id, item: it, target: node, facts: facts, action: d } }));
    } catch (e) { fail(e); }
    if (!d || !d.type) return;
    try {
      switch (d.type) {
        case "navigate": if (d.url) location.href = d.url; break;
        case "open": if (d.url) window.open(d.url, "_blank", "noopener"); break;
        case "copy": copyText(d.text || "", d.toast || "Copied."); break;
        case "post": post(d, it, kind, facts); break;
        case "htmx": viaHtmx(d, it, kind, facts); break;
        case "click":
          var target = d.selector ? (node.querySelector(d.selector) || doc.querySelector(d.selector)) : null;
          if (target) target.click(); else toast("Nothing here answers to that control.", "error");
          break;
        case "ask": askAgent(d.question || ""); break;
        case "emit":
          if (d.event) doc.dispatchEvent(new CustomEvent(d.event, { detail: {
            kind: kind, id: facts.id, target: node, facts: facts, detail: d.detail || null, item: it } }));
          break;
        case "reload": location.reload(); break;
        default: toast("This menu does not know how to “" + d.type + "”.", "error");
      }
    } catch (e) { fail(e); toast("That action failed: " + (e && e.message || e), "error"); }
  }
  function copyText(text, say) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { toast(say, "ok"); },
        function () { legacyCopy(text, say); });
    } else legacyCopy(text, say);
  }
  function legacyCopy(text, say) {
    // An internal http:// origin has no clipboard API; the old selection copy still works.
    var ta = el("textarea"); ta.value = text; ta.setAttribute("readonly", "");
    ta.style.position = "fixed"; ta.style.left = "-9999px";
    doc.body.appendChild(ta); ta.select();
    var ok = false;
    try { ok = doc.execCommand("copy"); } catch (e) {}
    ta.remove();
    toast(ok ? say : "Could not copy on this page. Select it and copy by hand.", ok ? "ok" : "error");
  }
  function finish(d, it, kind, facts, status, bodyText) {
    try {
      doc.dispatchEvent(new CustomEvent("hub-menu:done", { detail: {
        kind: kind, id: facts.id, item: it, status: status, body: bodyText, then: d.then || null } }));
    } catch (e) { fail(e); }
    var then = d.then;
    if (!then) return;
    if (then === "reload" || then.reload) { location.reload(); return; }
    if (then.htmx && window.htmx) {
      var t = then.htmx;
      htmx.ajax((t.method || "GET").toUpperCase(), t.url || location.href,
                { target: t.target || "body", swap: t.swap || "innerHTML", values: t.values || {} });
    }
    if (then.event) {
      var tgt = then.target ? doc.querySelector(then.target) : doc;
      if (tgt) tgt.dispatchEvent(new CustomEvent(then.event, { bubbles: true, detail: { kind: kind, id: facts.id } }));
    }
    if (then.navigate) location.href = then.navigate;
  }
  function describeError(status, text) {
    var msg = "";
    try { var j = JSON.parse(text); msg = j.error || j.detail || j.message || ""; } catch (e) {}
    if (!msg) {
      var plain = String(text || "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
      msg = plain.length > 160 ? plain.slice(0, 160) + "…" : plain;
    }
    if (status === 403) return "Not allowed" + (msg ? ": " + msg : ".");
    if (status === 401) return "Your session ended. Sign in again.";
    return (msg || "The server answered HTTP " + status) ;
  }
  function post(d, it, kind, facts) {
    var values = d.values || {};
    var form = new URLSearchParams();
    Object.keys(values).forEach(function (k) { form.append(k, values[k] == null ? "" : String(values[k])); });
    fetch(d.url, {
      method: (d.method || "POST").toUpperCase(), credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": csrfToken(),
                 "X-Requested-With": "XMLHttpRequest", "HX-Request": "true",
                 "Accept": "application/json, text/html;q=0.9, */*;q=0.5" },
      body: form.toString()
    }).then(function (r) {
      return r.text().then(function (t) {
        if (!r.ok) { toast(describeError(r.status, t), "error"); return; }
        // 204 is the app saying "nothing to do" (already that value, no one signed in);
        // a success toast over it would claim a change that did not happen.
        if (r.status === 204) { if (!r.headers.get("HX-Trigger")) toast("Nothing changed.", "ok"); return; }
        if (d.toast && !r.headers.get("HX-Trigger")) toast(fill(d.toast, facts), "ok");
        finish(d, it, kind, facts, r.status, t);
      });
    }).catch(function (err) { toast("Could not reach this app: " + (err && err.message || err), "error"); });
  }
  function viaHtmx(d, it, kind, facts) {
    if (!window.htmx) { if (d.url) location.href = d.url; return; }
    // htmx.ajax's promise resolves whatever the server said, so success is read off the
    // request itself. Only a 2xx that carried content is "done": a 204 or a response
    // carrying HX-Trigger is the APP speaking for itself (its own toast, its own "sign in
    // first"), and a 4xx/5xx is a refusal said in the server's own words. A 204 that meant "nobody
    // signed in" once toasted as a success the app never performed.
    var settled = false;
    function onAfter(e) {
      var det = e && e.detail || {};
      var cfg = det.requestConfig || {};
      if (settled || !det.xhr || (cfg.path && cfg.path !== d.url)) return;
      settled = true;
      doc.body.removeEventListener("htmx:afterRequest", onAfter);
      var x = det.xhr, status = x.status;
      if (status >= 400 || status === 0) { toast(describeError(status, x.responseText || ""), "error"); return; }
      var appSpoke = status === 204 || !!(x.getResponseHeader && x.getResponseHeader("HX-Trigger"));
      if (!appSpoke && d.toast) toast(fill(d.toast, facts), "ok");
      if (status !== 204) finish(d, it, kind, facts, status, "");
    }
    doc.body.addEventListener("htmx:afterRequest", onAfter);
    htmx.ajax((d.method || "GET").toUpperCase(), d.url, {
      target: d.target || "body", swap: d.swap || "innerHTML", values: d.values || {}
    });
  }
  function askAgent(question) {
    if (!question) return;
    var ok = false;
    try { ok = doc.dispatchEvent(new CustomEvent("hub:agent-ask", { cancelable: true, detail: { question: question } })); } catch (e) {}
    // The agent component answers by cancelling the event once it has taken the
    // question. An older agent that does not know the event leaves it uncancelled.
    if (ok) {
      copyText(question, "The agent on this page cannot take a question directly yet; the question is on your clipboard.");
    }
  }

  /* ----------------------------------------------------------------- close */
  function close() {
    var m = doc.getElementById(ID);
    if (m) m.remove();
    openState = null; confirmArmed = null;
  }

  /* -------------------------------------------------------------- gestures */
  function openFor(hit, x, y, viaKeyboard) {
    try { return build(hit.kind, hit.node, x, y, viaKeyboard); }
    catch (e) { fail(e); close(); return false; }
  }
  function pageKindHit(from) {
    // Right-click on nothing in particular: the "page" kind, if the app declared one,
    // but never inside a form field, a link, a button or another component's surface.
    if (!KINDS.page) return null;
    if (from.closest("input, textarea, select, [contenteditable], a[href], button, .ab-band, .ab-box, .hub-agent-panel, .hcm")) return null;
    var scope = KINDS.page.match ? from.closest(KINDS.page.match) : doc.body;
    return scope ? { kind: "page", node: scope } : null;
  }
  doc.addEventListener("contextmenu", function (e) {
    if (!(e.target instanceof Element)) return;
    // People right-click a text field for the browser's own menu; leave that alone.
    if (e.target.closest("input, textarea, select, [contenteditable]")) return;
    if (e.target.closest("[data-hcm-native]")) return;
    var hit = findTarget(e.target) || pageKindHit(e.target);
    if (!hit) return;
    var x = e.clientX, y = e.clientY;
    // Shift+F10 and the Menu key arrive as a contextmenu event at (0,0) in some
    // browsers; place beside the element instead of the corner.
    var viaKeyboard = (x === 0 && y === 0) || e.button === -1;
    if (openFor(hit, x, y, viaKeyboard)) e.preventDefault();
  });
  doc.addEventListener("keydown", function (e) {
    var menu = doc.getElementById(ID);
    if (!menu) {
      if ((e.key === "F10" && e.shiftKey) || e.key === "ContextMenu") {
        var a = doc.activeElement;
        if (a && a !== doc.body) {
          var hit = findTarget(a);
          if (hit && openFor(hit, 0, 0, true)) e.preventDefault();
        }
      }
      return;
    }
    if (e.key === "Escape") { e.preventDefault(); var back = openState && openState.node; close(); if (back && back.focus) try { back.focus({ preventScroll: true }); } catch (x) {} return; }
    if (e.key === "Tab" && menu._hcmTabs) {
      e.preventDefault();
      var t = menu._hcmTabs, cur = 0;
      t.panes.forEach(function (p, i) { if (p.tab.getAttribute("aria-selected") === "true") cur = i; });
      t.select((cur + (e.shiftKey ? -1 : 1) + t.panes.length) % t.panes.length, true);
      return;
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === "Home" || e.key === "End") {
      e.preventDefault();
      var scope = doc.activeElement && doc.activeElement.closest(".hcm-panel") || menu;
      var items = Array.from(scope.querySelectorAll(":scope .hcm-item")).filter(function (b) {
        return b.offsetParent && !b.disabled && (scope !== menu || !b.closest(".hcm-panel"));
      });
      if (!items.length) return;
      var i = items.indexOf(doc.activeElement);
      var next = e.key === "Home" ? items[0] : e.key === "End" ? items[items.length - 1]
               : items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length];
      if (next) next.focus();
      return;
    }
    // Type-ahead: a letter jumps to the next item starting with it.
    if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      var sc = doc.activeElement && doc.activeElement.closest(".hcm-panel") || menu;
      var all = Array.from(sc.querySelectorAll(".hcm-item")).filter(function (b) { return b.offsetParent && !b.disabled; });
      var from = all.indexOf(doc.activeElement);
      for (var k = 1; k <= all.length; k++) {
        var cand = all[(from + k) % all.length];
        if ((cand.textContent || "").trim().toLowerCase().charAt(0) === e.key.toLowerCase()) { cand.focus(); break; }
      }
    }
  });
  doc.addEventListener("pointerdown", function (e) {
    var menu = doc.getElementById(ID);
    if (menu && !menu.contains(e.target)) close();
  }, true);
  // Touch: a long press opens it where the finger is.
  var press = null;
  doc.addEventListener("pointerdown", function (e) {
    if (e.pointerType !== "touch" || !(e.target instanceof Element)) return;
    var hit = findTarget(e.target);
    if (!hit) return;
    var sx = e.clientX, sy = e.clientY;
    press = setTimeout(function () { press = null; if (openFor(hit, sx, sy, false)) { try { e.preventDefault(); } catch (x) {} } }, 550);
    var cancel = function (ev) {
      if (press && (ev.type !== "pointermove" || Math.hypot(ev.clientX - sx, ev.clientY - sy) > 8)) { clearTimeout(press); press = null; }
      if (ev.type !== "pointermove") { doc.removeEventListener("pointermove", cancel); doc.removeEventListener("pointerup", cancel); doc.removeEventListener("pointercancel", cancel); }
    };
    doc.addEventListener("pointermove", cancel); doc.addEventListener("pointerup", cancel); doc.addEventListener("pointercancel", cancel);
  }, { passive: true });
  // CLOSE ON SCROLL ONLY WHEN THE THING MOVED. Any scroll used to close it, and a page
  // that nudges its own scroller on the same gesture (a register page focuses the
  // row, which scrolls a pixel) shut the menu before it was ever seen. A menu over a
  // target that has not moved is still pointing at the right thing.
  window.addEventListener("scroll", function (e) {
    if (!openState) return;
    if (e.target && e.target.nodeType === 1 && openState.menu.contains(e.target)) return;
    var n = openState.node;
    if (!n || !doc.contains(n)) { close(); return; }
    var r = n.getBoundingClientRect();
    if (Math.abs(r.top - openState.top) > 2 || Math.abs(r.left - openState.left) > 2) close();
  }, true);
  window.addEventListener("resize", close);
  window.addEventListener("blur", close);
  // A live page swapping the target out from under an open menu (htmx settle, SSE) closes it:
  // a menu for a card that is no longer there would write to the wrong thing.
  doc.addEventListener("htmx:afterSettle", function () { if (openState && !doc.contains(openState.node)) close(); });

  /* ----------------------------------------------------------------- public */
  window.HubMenu = {
    version: VERSION,
    open: function (target, x, y) {
      var node = typeof target === "string" ? doc.querySelector(target) : target;
      var hit = node ? findTarget(node) : null;
      return hit ? openFor(hit, x || 0, y || 0, x == null) : false;
    },
    close: close,
    register: function (kind, def) {
      if (!kind || !def) return;
      KINDS[kind] = def;
      if (SMART[kind]) delete SMART[kind];
    },
    kinds: function () { return Object.keys(KINDS); },
    smart: function () { var out = {}; Object.keys(SMART).forEach(function (k) { out[k] = { state: SMART[k].state, reason: SMART[k].reason || "", error: SMART[k].error || "", ms: SMART[k].ms || 0 }; }); return out; }
  };

  // The prefetch waits for the page to settle: the cards may still be rendering.
  if (doc.readyState === "complete") setTimeout(prefetch, 400);
  else window.addEventListener("load", function () { setTimeout(prefetch, 400); });

  try { doc.dispatchEvent(new CustomEvent("hub-menu:ready", { detail: { version: VERSION, kinds: Object.keys(KINDS) } })); }
  catch (e) {}
})();
