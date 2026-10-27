/* Agent interface — a hosted component. Link it; never copy it.
 *
 *   <link rel="stylesheet" href="/hub/components/agent/agent.css">
 *   <script src="/hub/components/agent/agent.js" defer></script>
 *   <div data-hub-agent data-app="budget-app" data-ask="/budget/agent/ask"
 *        data-history="/budget/agent/history" data-conversation="/budget/agent/conversation"
 *        data-profile="/budget/profile" data-props="/hub/components/props/budget-app.json"></div>
 *
 * It talks ONLY to the app's own bridge endpoints (same origin, the app's own sign-in). The app's
 * server forwards to the hub with its hub credential; the agent key stays at the hub.
 *
 * Every state is drawn as itself: an unconfigured lane says what is missing, a failure says what
 * failed and offers a retry, an empty conversation says what the agent is for. An empty answer is
 * never shown as a blank bubble. Text from the agent is set with textContent, never as HTML.
 */
(function () {
  "use strict";
  var doc = document, win = window;
  var MIN_W = 320, DEFAULT_W = 400;

  function store(key, value) {
    try {
      if (value === undefined) return win.localStorage.getItem(key);
      if (value === null) win.localStorage.removeItem(key); else win.localStorage.setItem(key, value);
    } catch (e) { /* private window or blocked storage: the panel still works, it just forgets */ }
    return null;
  }
  function el(tag, attrs, kids) {
    var n = doc.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v == null || v === false) return;
      if (k === "text") n.textContent = v;
      else if (k === "class") n.className = v;
      else if (k.slice(0, 2) === "on") n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    });
    (kids || []).forEach(function (c) { if (c) n.appendChild(typeof c === "string" ? doc.createTextNode(c) : c); });
    return n;
  }
  function csrf() {
    var m = /(?:^|;\s*)csrftoken=([^;]+)/.exec(doc.cookie || "");
    return m ? decodeURIComponent(m[1]) : "";
  }
  function getJSON(url) {
    return fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) {
        return r.json().catch(function () { return { ok: false, reason: "failed", error: "HTTP " + r.status + " (not JSON)" }; })
          .then(function (b) { if (!r.ok && b && b.ok === undefined) b.ok = false; return b; });
      });
  }
  function postJSON(url, body) {
    return fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": csrf() },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () {
        return { ok: false, reason: "failed", error: "The app's bridge answered HTTP " + r.status + " with no JSON." };
      });
    });
  }
  // The hub wraps authenticated answers in {data: …}; a bridge may pass either shape through.
  function unwrap(b) { return b && b.data && b.ok === undefined ? b.data : b; }

  /* ---- the page the reader is looking at ----
     ON BY DEFAULT, ONE CLICK OFF, remembered for every app (hub-agent:page): it is the
     reader's habit, not one app's property. Read at the MOMENT OF ASKING and only what is ON
     SCREEN: title, address (secret-looking query values dropped), the section the nav marks as
     current, selected text, open panels and dialogs first (a drawer is usually what the question
     is about), then the visible text in reading order -- one line per block, a table row as
     "a | b | c" -- and visible form fields with their values. Never password or hidden fields,
     the shared banner, this panel, or anything marked [data-agent-skip]. At most PAGE_MAX
     characters, sent as ONE thread item {role: "page"}: every app's bridge already forwards
     `thread` untouched, so every adopter gains it with no change, and the hub lifts it out
     (hub_core/agent_broker.compose) -- it is never a turn. */
  var PAGE_KEY = "hub-agent:page", PAGE_MAX = 7000;
  var GUIDE_OPEN_KEY = "hub-agent:guide-open";
  var GUIDE_KINDS = { system: "Automatic", user: "Entered by people", mixed: "Both" };
  function pageOn() { return store(PAGE_KEY) !== "0"; }
  var AGENT_PARTS = ".hub-agent-panel, .hub-agent-launch, [data-hub-agent]";
  var PAGE_SKIP = "script, style, noscript, template, svg, [hidden], [aria-hidden='true'], [data-agent-skip], " + AGENT_PARTS;
  var PAGE_SKIP_MAIN = PAGE_SKIP + ", #app-banner, .ab-surface";
  var PAGE_BLOCK = "tr, li, p, h1, h2, h3, h4, h5, h6, dt, dd, label, legend, caption, figcaption, " +
                   "summary, th, blockquote, pre, button, a, option";
  function clip(s, n) {
    s = String(s || "").replace(/\s+/g, " ").trim();
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }
  function shown(n) {
    if (!n.getClientRects().length) return false;
    var cs = getComputedStyle(n);
    return cs.visibility !== "hidden" && parseFloat(cs.opacity) > 0.05;
  }
  function onScreen(r) {
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 && r.top < win.innerHeight && r.left < win.innerWidth;
  }
  function outside(n, root, skipInside) {
    return !(skipInside || []).some(function (x) { return x !== root && x.contains(n); });
  }
  function screenText(root, skip, budget, skipInside) {
    var lines = [], cur = "", curBlock = null, curCell = null, used = 0, ok = new Map();
    function flush() { if (cur) { lines.push(cur); used += cur.length + 1; } cur = ""; }
    var walker = doc.createTreeWalker(root, 4), t;
    while ((t = walker.nextNode()) && used < budget) {
      var s = t.nodeValue.replace(/\s+/g, " ").trim();
      var p = t.parentElement;
      if (!s || !p || !/[A-Za-z0-9]/.test(s)) continue;     // a lone glyph is not text
      var lab = p.closest("label");
      if (lab && lab.control) continue;                      // read with its field, below
      if (!ok.has(p)) ok.set(p, !p.closest(skip) && outside(p, root, skipInside) && onScreen(p.getBoundingClientRect()) && shown(p));
      if (!ok.get(p)) continue;
      var row = p.closest("tr"), cell = row ? p.closest("td, th") : null;
      var block = row || p.closest(PAGE_BLOCK) || p;
      if (block !== curBlock) { flush(); curBlock = block; curCell = cell; cur = s; }
      else { cur += (row && cell !== curCell ? " | " : " ") + s; curCell = cell; }
    }
    flush();
    return lines;
  }
  function fieldsOnScreen(root, skip, skipInside) {
    var out = [];
    Array.prototype.forEach.call(root.querySelectorAll("input, select, textarea"), function (f) {
      if (out.length >= 40 || f.closest(skip) || !outside(f, root, skipInside)) return;
      var type = String(f.type || "").toLowerCase();
      if (/^(password|hidden|submit|button|reset|file|image)$/.test(type)) return;
      if (!shown(f) || !onScreen(f.getBoundingClientRect())) return;
      var label = f.getAttribute("aria-label") || (f.labels && f.labels[0] && f.labels[0].textContent) || f.placeholder || f.name || f.id || type;
      var v;
      if (type === "checkbox" || type === "radio") {
        if (type === "radio" && !f.checked) return;
        v = type === "radio" ? "chosen" : (f.checked ? "checked" : "not checked");
      } else if (f.tagName === "SELECT") {
        v = Array.prototype.filter.call(f.options, function (o) { return o.selected; }).map(function (o) { return o.text; }).join(", ");
      } else v = f.value;
      out.push(clip(label, 60) + ": " + (clip(v, 200) || "(empty)"));
    });
    return out;
  }
  // Open panels and dialogs on screen, outermost only.
  function openPanels() {
    var found = Array.prototype.filter.call(doc.querySelectorAll("dialog[open], [role=dialog], [aria-modal=true], aside"), function (n) {
      if (n.closest(AGENT_PARTS + ", [data-agent-skip], #app-banner, .ab-sb") || !shown(n)) return false;
      var pos = getComputedStyle(n).position;
      return (pos === "fixed" || pos === "absolute" || n.tagName === "DIALOG") && onScreen(n.getBoundingClientRect());
    });
    return found.filter(function (n) { return !found.some(function (o) { return o !== n && o.contains(n); }); }).slice(0, 3);
  }
  function safeAddress() {
    var q = [];
    try {
      new URLSearchParams(win.location.search).forEach(function (v, k) {
        q.push(encodeURIComponent(k) + "=" + (/token|key|secret|pass|code|session|auth/i.test(k) ? "…" : encodeURIComponent(v)));
      });
    } catch (e) { /* an unparseable query is simply not described */ }
    return win.location.pathname + (q.length ? "?" + q.join("&") : "");
  }
  function pageContext(panel) {
    var parts = ["Page: " + clip(doc.title, 160), "Address: " + safeAddress()];
    var here = doc.querySelector("[aria-current=page], [aria-current=true]");
    if (here && clip(here.textContent, 80)) parts.push("Section: " + clip(here.textContent, 80));
    try {
      var sel = win.getSelection ? win.getSelection() : null;
      if (sel && sel.rangeCount && !(panel && panel.contains(sel.anchorNode))) {
        var st = clip(String(sel), 1500);
        if (st) parts.push("Selected text: " + st);
      }
    } catch (e) { /* no selection API: nothing selected */ }
    var panels = openPanels();
    panels.forEach(function (pn) {
      var hd = pn.querySelector("h1, h2, h3, [class*=title]");
      var name = clip(pn.getAttribute("aria-label") || (hd && hd.textContent) || "", 60);
      var lines = screenText(pn, PAGE_SKIP, 1800).concat(fieldsOnScreen(pn, PAGE_SKIP));
      if (lines.length) parts.push("Open panel" + (name ? " (" + name + ")" : "") + ":\n" + lines.join("\n"));
    });
    var main = doc.querySelector("main, [role=main]") || doc.body;
    var lines = screenText(main, PAGE_SKIP_MAIN, PAGE_MAX, panels);
    var fields = fieldsOnScreen(main, PAGE_SKIP_MAIN, panels);
    if (lines.length) parts.push("On screen:\n" + lines.join("\n"));
    if (fields.length) parts.push("Fields on screen:\n" + fields.join("\n"));
    var out = parts.join("\n\n");
    return out.length > PAGE_MAX ? out.slice(0, PAGE_MAX - 1) + "…" : out;
  }
  function guideTarget(sel) {
    if (!sel) return null;
    try { return doc.querySelector(String(sel)); } catch (e) { return null; }
  }
  function showGuideTarget(sel) {
    var t = guideTarget(sel);
    if (!t) return;
    var still = win.matchMedia && win.matchMedia("(prefers-reduced-motion: reduce)").matches;
    try { t.scrollIntoView({ block: "center", behavior: still ? "auto" : "smooth" }); } catch (e) { t.scrollIntoView(); }
    t.setAttribute("data-hub-agent-mark", "1");
    setTimeout(function () { t.removeAttribute("data-hub-agent-mark"); }, 1800);
  }

  var DEFAULTS = {
    greeting: "Ask about this app",
    intro: "Ask a question in your own words. Answers cite where they came from when the agent can.",
    prompts: [], placeholder: "Ask a question", entry: ["float"]
  };

  function Agent(mount) {
    var d = mount.dataset;
    this.mount = mount;
    this.app = (d.app || "app").toLowerCase();
    this.urls = { ask: d.ask, history: d.history, conversation: d.conversation, profile: d.profile, props: d.props };
    this.title = d.title || "Assistant";
    this.key = "hub-agent:" + this.app + ":";
    this.props = Object.assign({}, DEFAULTS);
    this.placement = "";
    this.thread = [];
    this.conversationId = store(this.key + "conversation") || "";
    this.width = Math.max(MIN_W, parseInt(store(this.key + "width") || DEFAULT_W, 10) || DEFAULT_W);
    this.busy = false;
    this.build();
    this.load();
  }

  Agent.prototype.build = function () {
    var self = this;
    this.log = el("div", { class: "hub-agent-log", role: "log", "aria-live": "polite", "aria-relevant": "additions" });
    this.input = el("textarea", { class: "hub-agent-input", rows: "2", "aria-label": "Your question" });
    this.input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); self.send(); }
    });
    this.sendBtn = el("button", { class: "hub-agent-send", type: "button", text: "Ask", onclick: function () { self.send(); } });
    /* THE PAGE CHIP: "Reading this page" (on by default, one click off, remembered for every
       app) and "What it reads", which shows the exact text a question would carry. */
    this.pageLabel = el("span", { class: "hub-agent-page-label" });
    this.pageToggle = el("button", { class: "hub-agent-page-toggle", type: "button",
      onclick: function () { store(PAGE_KEY, pageOn() ? "0" : "1"); self.syncPage(); } }, [this.pageLabel]);
    this.pagePeek = el("pre", { class: "hub-agent-page-peek", tabindex: "0", hidden: true,
      "aria-label": "The text the agent reads from this page" });
    this.pageShow = el("button", { class: "hub-agent-page-show", type: "button", "aria-expanded": "false",
      text: "What it reads", onclick: function () { self.togglePeek(); } });
    this.form = el("div", { class: "hub-agent-compose" }, [
      el("div", { class: "hub-agent-page" }, [this.pageToggle, this.pageShow]),
      this.pagePeek,
      el("div", { class: "hub-agent-compose-row" }, [this.input, this.sendBtn])]);
    this.syncPage();
    /* THE PAGE GUIDE (folding, above the conversation) -- drawn from the app's own island. */
    this.guideToggle = el("button", { class: "hub-agent-guide-toggle", type: "button", "aria-expanded": "false",
      onclick: function () {
        var on = self.guideToggle.getAttribute("aria-expanded") !== "true";
        store(GUIDE_OPEN_KEY, on ? "1" : "0");
        self.setGuideOpen(on);
      } });
    this.guideBody = el("div", { class: "hub-agent-guide-body", hidden: true });
    this.guide = el("section", { class: "hub-agent-guide", "aria-label": "About this page", hidden: true },
      [this.guideToggle, this.guideBody]);
    this.pastBtn = el("button", { class: "hub-agent-tool", type: "button", "aria-pressed": "false",
      text: "Past chats", onclick: function () { self.togglePast(); } });
    this.panel = el("aside", { class: "hub-agent-panel", role: "complementary", "aria-label": this.title, hidden: true }, [
      el("div", { class: "hub-agent-grip", role: "separator", "aria-orientation": "vertical",
        "aria-label": "Resize the panel", tabindex: "0" }),
      el("div", { class: "hub-agent-head" }, [
        el("span", { class: "hub-agent-title", text: this.title }),
        el("span", { class: "hub-agent-tools" }, [
          this.pastBtn,
          el("button", { class: "hub-agent-tool", type: "button", text: "New chat", onclick: function () { self.newChat(); } }),
          el("button", { class: "hub-agent-tool hub-agent-close", type: "button", "aria-label": "Close the panel",
            text: "×", onclick: function () { self.close(); } })
        ])
      ]),
      this.past = el("div", { class: "hub-agent-past", hidden: true }),
      this.guide,
      this.log,
      this.form
    ]);
    doc.body.appendChild(this.panel);
    this.bindResize(this.panel.querySelector(".hub-agent-grip"));
    this.panel.addEventListener("keydown", function (e) { if (e.key === "Escape") self.close(); });
    doc.addEventListener("click", function (e) {
      var t = e.target && e.target.closest && e.target.closest("[data-hub-agent-open]");
      if (t) { e.preventDefault(); self.open(); }
    });
    win.addEventListener("hub:agent-open", function (e) {
      self.open();
      var q = e.detail && e.detail.question;
      if (q) { self.input.value = q; self.send(); }
    });
    /* ASK FROM ANYWHERE ON THE PAGE. Another component (the context menu's "Ask the agent")
       dispatches hub:agent-ask {question} on document; the panel opens and asks it exactly
       as if the person had typed it, and CANCELS the event so the sender knows it was taken
       (an older agent leaves it uncancelled and the sender falls back). A question already in
       flight is not interrupted: the new one waits in the box for the person to send. */
    doc.addEventListener("hub:agent-ask", function (e) {
      var q = e && e.detail && String(e.detail.question || "").trim();
      if (!q) return;
      e.preventDefault();
      if (!self.isOpen) self.open(true);
      self.input.value = q;
      if (!self.busy) self.send(); else self.input.focus();
    });
    win.addEventListener("hub:component-props", function (e) {
      var det = e.detail || {};
      if (det.app && det.app !== self.app) return;
      self.applyProps((det.props || {}).agent || det.agent || {});
    });
    /* A page that changes what it shows without a reload says so: detail {guide} draws that
       guide, no detail re-reads the island. */
    doc.addEventListener("hub:agent-guide", function (e) {
      self.drawGuide(e && e.detail && e.detail.guide !== undefined ? e.detail.guide : self.readGuide());
    });
    // Booted at DOMContentLoaded (or later), so the targets a guide points at exist by now.
    this.drawGuide(this.readGuide());
    this.renderEmpty();
  };

  /* ---- the page guide ----
     The APP's words about the page, per page, as a JSON island; this component only draws
     them. kind says who fills a section in: system (the app, by itself), user (a person) or
     mixed. Every kind is a WORD on a chip, never colour alone, and the legend lists only the
     kinds this page has. A section whose target is on the page gets a button that scrolls to
     it and outlines it, so "which box is that?" is answered on the page. No island, or no
     sections, draws nothing. Folded or open is remembered for every app (hub-agent:guide-open):
     a person who has learned the pages folds it once, not once per app. */
  Agent.prototype.readGuide = function () {
    var node = this.mount.querySelector('script[type="application/json"][data-agent-guide]') ||
               doc.querySelector('script[type="application/json"][data-agent-guide]');
    if (!node) return null;
    try { return JSON.parse(node.textContent || "null"); }
    catch (e) { console.warn("[hub-agent] the page guide island is not valid JSON and was ignored."); return null; }
  };
  Agent.prototype.setGuideOpen = function (on) {
    this.guideToggle.setAttribute("aria-expanded", on ? "true" : "false");
    this.guideBody.hidden = !on;
    this.guide.setAttribute("data-open", on ? "1" : "0");
  };
  Agent.prototype.drawGuide = function (g) {
    this.guideToggle.textContent = ""; this.guideBody.textContent = "";
    var sections = g && Array.isArray(g.sections) ? g.sections.filter(function (s) { return s && s.name; }) : [];
    if (!sections.length) { this.guide.hidden = true; return; }
    var labels = {};
    Object.keys(GUIDE_KINDS).forEach(function (k) {
      labels[k] = g.labels && g.labels[k] ? String(g.labels[k]) : GUIDE_KINDS[k];
    });
    this.guideToggle.appendChild(el("span", { class: "hub-agent-guide-words" }, [
      el("span", { class: "hub-agent-guide-kicker", text: g.kicker ? String(g.kicker) : "About this page" }),
      g.title ? el("span", { class: "hub-agent-guide-title", text: String(g.title) }) : null]));
    this.guideToggle.appendChild(el("span", { class: "hub-agent-guide-chev", "aria-hidden": "true" }));
    if (g.summary) this.guideBody.appendChild(el("p", { class: "hub-agent-guide-summary", text: String(g.summary) }));
    var seen = {};
    sections.forEach(function (s) { seen[GUIDE_KINDS[s.kind] ? s.kind : "mixed"] = true; });
    this.guideBody.appendChild(el("div", { class: "hub-agent-guide-legend" }, Object.keys(GUIDE_KINDS)
      .filter(function (k) { return seen[k]; })
      .map(function (k) { return el("span", { class: "hub-agent-guide-chip", "data-kind": k, text: labels[k] }); })));
    this.guideBody.appendChild(el("ol", { class: "hub-agent-guide-list" }, sections.map(function (s) {
      var kind = GUIDE_KINDS[s.kind] ? s.kind : "mixed";
      var name = s.target && guideTarget(s.target)
        ? el("button", { class: "hub-agent-guide-name hub-agent-guide-jump", type: "button", title: "Show it on the page",
                         text: String(s.name), onclick: function () { showGuideTarget(s.target); } })
        : el("span", { class: "hub-agent-guide-name", text: String(s.name) });
      return el("li", { class: "hub-agent-guide-item", "data-kind": kind }, [
        el("div", { class: "hub-agent-guide-head" }, [name,
          el("span", { class: "hub-agent-guide-chip", "data-kind": kind, text: labels[kind] })]),
        s.text ? el("p", { class: "hub-agent-guide-text", text: String(s.text) }) : null,
        s.who ? el("p", { class: "hub-agent-guide-who", text: String(s.who) }) : null]);
    })));
    if (g.more && g.more.href) {
      this.guideBody.appendChild(el("a", { class: "hub-agent-guide-more", href: String(g.more.href),
        text: String(g.more.label || "More about this page") }));
    }
    this.guide.hidden = false;
    this.setGuideOpen(store(GUIDE_OPEN_KEY) !== "0");
  };

  /* ---- the page chip ---- */
  Agent.prototype.syncPage = function () {
    var on = pageOn();
    this.pageToggle.setAttribute("aria-pressed", on ? "true" : "false");
    this.pageLabel.textContent = on ? "Reading this page" : "Not reading this page";
    this.pageToggle.title = on ? "Your questions include what is on your screen. Click to stop."
                               : "Your questions do not include this page. Click to let the agent read it.";
    this.pageShow.hidden = !on;
    if (!on) { this.pagePeek.hidden = true; this.pageShow.setAttribute("aria-expanded", "false"); this.pageShow.textContent = "What it reads"; }
  };
  Agent.prototype.togglePeek = function () {
    var show = this.pagePeek.hidden;
    if (show) this.pagePeek.textContent = pageContext(this.panel);
    this.pagePeek.hidden = !show;
    this.pageShow.setAttribute("aria-expanded", show ? "true" : "false");
    this.pageShow.textContent = show ? "Hide" : "What it reads";
  };

  Agent.prototype.load = function () {
    var self = this, jobs = [];
    var preset = win.HubComponentProps && (win.HubComponentProps.agent || (win.HubComponentProps.props || {}).agent);
    if (preset) this.applyProps(preset);
    if (this.urls.props) {
      var sep = this.urls.props.indexOf("?") >= 0 ? "&" : "?";
      jobs.push(getJSON(this.urls.props + sep + "component=agent").then(function (b) {
        if (b && b.ok !== false && b.props) self.applyProps(b.props.agent || {});
      }).catch(function () { /* defaults stand; the launcher still draws */ }));
    }
    if (this.urls.profile) {
      jobs.push(getJSON(this.urls.profile).then(function (b) {
        var prefs = (unwrap(b) || {}).prefs || {};
        if (prefs.agent) self.placement = prefs.agent;
      }).catch(function () { /* no preference: the app's first entry point is used */ }));
    }
    Promise.all(jobs).then(function () { self.drawLaunchers(); });
    if (store(this.key + "open") === "1") this.open(true);
    if (this.conversationId && this.urls.conversation) this.loadConversation(this.conversationId, true);
  };

  Agent.prototype.applyProps = function (p) {
    var next = Object.assign({}, DEFAULTS);
    Object.keys(p || {}).forEach(function (k) {
      var v = p[k];
      if (v === "" || v == null || (Array.isArray(v) && !v.length && k !== "entry")) return;
      next[k] = v;
    });
    this.props = next;
    this.input.setAttribute("placeholder", next.placeholder);
    if (!this.thread.length) this.renderEmpty();
    if (this.launchersDrawn) this.drawLaunchers();
  };

  /* THE DOORS DO WHAT THE SETTINGS SAY. Every entry point the app ticked is drawn -- float,
     right-edge tab, header button -- one launcher per door, re-drawn whenever the properties
     change so an operator's preview is live. A person's own placement preference narrows that
     to their one door when the app allows it. None ticked = only the app's own
     [data-hub-agent-open]. (Choosing ONE launcher from the list drew the header button for
     "floating" and silently ignored the other ticks.) */
  Agent.prototype.drawLaunchers = function () {
    var self = this;
    this.launchersDrawn = true;
    (this.launchers || []).forEach(function (n) { if (n.parentNode) n.parentNode.removeChild(n); });
    this.launchers = [];
    var allowed = (Array.isArray(this.props.entry) ? this.props.entry : [])
      .filter(function (w) { return w === "float" || w === "sidebar" || w === "header"; });
    var slot = doc.querySelector("[data-hub-agent-header]");
    var doors = allowed.indexOf(this.placement) >= 0 ? [this.placement] : allowed.slice();
    if (!slot) doors = doors.filter(function (w) { return w !== "header"; });
    // The slot says whether its door is on, so an app can hide the hairline around it.
    if (slot) slot.setAttribute("data-hub-agent-door", doors.indexOf("header") >= 0 ? "on" : "off");
    var label = "Open " + this.title;
    doors.forEach(function (where) {
      var btn;
      if (where === "header") {
        btn = el("button", { class: "hub-agent-launch is-header", type: "button", "aria-label": label, text: self.title });
        slot.appendChild(btn);
      } else {
        btn = el("button", { class: "hub-agent-launch is-" + where, type: "button", "aria-label": label,
          title: where === "float" ? null : label }, [el("span", { class: "hub-agent-glyph", "aria-hidden": "true" }),
          where === "sidebar" ? el("span", { class: "hub-agent-tab-text", text: self.title })
                              : el("span", { class: "hub-agent-pop", "aria-hidden": "true", text: "Ask " + self.title })]);
        doc.body.appendChild(btn);
      }
      btn.addEventListener("click", function () { self.isOpen ? self.close() : self.open(); });
      self.launchers.push(btn);
    });
    this.syncLaunchers();
  };
  Agent.prototype.syncLaunchers = function () {
    var open = !!this.isOpen;
    (this.launchers || []).forEach(function (b) {
      b.setAttribute("aria-expanded", open ? "true" : "false");
      // The edge tab and the float step aside for the open panel: the panel carries its own
      // close, and a disc left floating beside it covers the page the reader is working on.
      b.classList.toggle("is-hidden", open && (b.classList.contains("is-sidebar") || b.classList.contains("is-float")));
    });
  };

  /* The panel is DOCKED: it pushes the page (the root gets a right margin equal to its width)
     rather than floating over the content a reader is asking about. */
  Agent.prototype.dock = function () {
    var root = doc.documentElement;
    var w = Math.min(this.width, Math.round(win.innerWidth * 0.7));
    if (win.innerWidth < 720) w = win.innerWidth;         // a phone gets the whole screen
    this.panel.style.width = w + "px";
    root.style.setProperty("--hub-agent-dock", this.isOpen && win.innerWidth >= 720 ? w + "px" : "0px");
    root.classList.toggle("hub-agent-docked", !!this.isOpen && win.innerWidth >= 720);
    this.docked = !!this.isOpen && win.innerWidth >= 720 ? w : 0;
    this.pushAside();
  };

  /* OTHER SIDEBARS STEP ASIDE. Docked, the panel takes its width from <body>, so the PAGE
     moves over -- but a FIXED layer ignores body margin, and an app's own drawer pinned to the
     right edge was drawn straight over the panel, or under it. Measured, not declared: every
     48px down the right edge, the nearest fixed layer under that point that reaches the edge --
     a sidebar, a sheet or bar across the foot, a toast; never the shared banner, never ours.
     Each one found is moved left by exactly the panel's width -- a right margin, or a translate
     for a layer placed by `left`, which ignores the margin; a window-wide layer that will not
     shrink is left alone rather than hung off the left -- so both stay usable side by side.
     Re-checked while docked whenever the page changes (a drawer opened after the panel is moved
     too) and on every resize of the panel, and every one is put back exactly as it was when the
     panel closes. A layer that already adds --hub-agent-dock to its own `right` is not on the
     edge, so it is never moved twice. */
  Agent.prototype.edgeLayer = function (n, W) {
    for (; n && n !== doc.body && n !== doc.documentElement; n = n.parentElement) {
      if (n.closest(AGENT_PARTS + ", #app-banner")) return null;
      var cs = getComputedStyle(n);
      if (cs.position !== "fixed") continue;
      var r = n.getBoundingClientRect();
      if (r.right < W - 4 || r.width < 40 || r.height < 8) return null;
      if (cs.visibility === "hidden" || parseFloat(cs.opacity) < 0.05) return null;
      return n;
    }
    return null;
  };
  Agent.prototype.pushAside = function () {
    var self = this, w = this.docked || 0;
    this.pushed = (this.pushed || []).filter(function (p) { return p.el.isConnected; });
    if (!w) {
      this.pushed.forEach(function (p) { p.el.style[p.prop] = p.prev; });
      this.pushed = [];
      return;
    }
    var W = win.innerWidth, H = win.innerHeight, ys = [];
    for (var y = 24; y < H; y += 48) ys.push(y);
    ys.push(H - 4);
    if (doc.elementsFromPoint) ys.forEach(function (y) {
      doc.elementsFromPoint(W - 3, y).some(function (n) {
        var s = self.edgeLayer(n, W);
        if (!s) return false;
        if (self.pushed.some(function (p) { return p.el === s; })) return true;
        var before = s.getBoundingClientRect().right;
        var rec = { el: s, prop: "marginRight", prev: s.style.marginRight };
        s.style.marginRight = w + "px";
        if (before - s.getBoundingClientRect().right < 4) {
          s.style.marginRight = rec.prev;
          // A window-wide layer that will not shrink is left alone: sliding it would hang it
          // off the other side.
          if (s.getBoundingClientRect().width > W * 0.9) return true;
          rec = { el: s, prop: "translate", prev: s.style.translate };
        }
        self.pushed.push(rec);
        return true;
      });
    });
    // Only write what differs, so this observer's own writes do not wake it again.
    this.pushed.forEach(function (p) {
      var v = p.prop === "translate" ? (-w) + "px 0" : w + "px";
      if (p.el.style[p.prop] !== v) p.el.style[p.prop] = v;
    });
  };
  Agent.prototype.watchEdge = function () {
    var self = this, queued = false;
    if (this._edgeWatch || !win.MutationObserver) return;
    function soon() {
      if (queued) return;
      queued = true;
      requestAnimationFrame(function () { queued = false; self.pushAside(); });
    }
    this._edgeWatch = new MutationObserver(function (list) {
      if (!self.docked && !(self.pushed || []).length) return;
      for (var i = 0; i < list.length; i++) { if (!self.panel.contains(list[i].target)) { soon(); return; } }
    });
    this._edgeWatch.observe(doc.body, { attributes: true, childList: true, subtree: true,
      attributeFilter: ["class", "style", "hidden", "open", "aria-hidden", "data-open"] });
    doc.addEventListener("transitionend", function (e) { if (!self.panel.contains(e.target)) soon(); }, true);
  };
  Agent.prototype.open = function (quiet) {
    this.isOpen = true;
    this.panel.hidden = false;
    store(this.key + "open", "1");
    this.dock();
    this.watchEdge();
    this.syncLaunchers();
    if (!quiet) this.input.focus();
    var self = this;
    if (!this._onResize) { this._onResize = function () { self.dock(); }; win.addEventListener("resize", this._onResize); }
  };
  Agent.prototype.close = function () {
    this.isOpen = false;
    this.panel.hidden = true;
    store(this.key + "open", null);
    this.dock();
    this.syncLaunchers();
    var first = (this.launchers || [])[0];
    if (first) first.focus();
  };
  Agent.prototype.bindResize = function (grip) {
    var self = this;
    function setW(w) {
      self.width = Math.max(MIN_W, Math.min(Math.round(w), Math.round(win.innerWidth * 0.7)));
      store(self.key + "width", String(self.width));
      self.dock();
    }
    grip.addEventListener("pointerdown", function (e) {
      e.preventDefault();
      grip.setPointerCapture(e.pointerId);
      self.panel.classList.add("is-resizing");
      function move(ev) { setW(win.innerWidth - ev.clientX); }
      function up() {
        self.panel.classList.remove("is-resizing");
        grip.removeEventListener("pointermove", move);
        grip.removeEventListener("pointerup", up);
      }
      grip.addEventListener("pointermove", move);
      grip.addEventListener("pointerup", up);
    });
    grip.addEventListener("keydown", function (e) {
      if (e.key === "ArrowLeft") { setW(self.width + 24); e.preventDefault(); }
      if (e.key === "ArrowRight") { setW(self.width - 24); e.preventDefault(); }
    });
  };

  /* ---- conversation ---- */
  Agent.prototype.renderEmpty = function () {
    if (this.thread.length) return;
    var self = this;
    this.log.textContent = "";
    var chips = (this.props.prompts || []).map(function (q) {
      return el("button", { class: "hub-agent-chip", type: "button", text: q,
        onclick: function () { self.input.value = q; self.send(); } });
    });
    this.log.appendChild(el("div", { class: "hub-agent-empty" }, [
      el("h2", { class: "hub-agent-greet", text: this.props.greeting }),
      el("p", { text: this.props.intro }),
      chips.length ? el("div", { class: "hub-agent-chips" }, chips) : null
    ]));
  };
  Agent.prototype.bubble = function (role, text, extra) {
    if (!this.thread.length && this.log.querySelector(".hub-agent-empty")) this.log.textContent = "";
    var body = el("div", { class: "hub-agent-text" });
    String(text || "").split(/\n{2,}/).forEach(function (para) { body.appendChild(el("p", { text: para })); });
    var node = el("div", { class: "hub-agent-msg is-" + role }, [
      el("span", { class: "hub-agent-who", text: role === "you" ? "You" : this.title }), body, extra || null]);
    this.log.appendChild(node);
    this.log.scrollTop = this.log.scrollHeight;
    return node;
  };
  Agent.prototype.notice = function (kind, title, detail, retry) {
    var self = this;
    var node = el("div", { class: "hub-agent-notice is-" + kind, role: kind === "failed" ? "alert" : "status" }, [
      el("strong", { text: title }), el("span", { text: detail || "" }),
      retry ? el("button", { class: "hub-agent-tool", type: "button", text: "Try again",
        onclick: function () { node.parentNode && node.parentNode.removeChild(node); retry(); } }) : null
    ]);
    if (this.log.querySelector(".hub-agent-empty")) this.log.textContent = "";
    this.log.appendChild(node);
    this.log.scrollTop = this.log.scrollHeight;
    self.lastNotice = node;
  };
  Agent.prototype.newChat = function () {
    this.thread = [];
    this.conversationId = "";
    store(this.key + "conversation", null);
    this.renderEmpty();
    this.input.focus();
  };
  Agent.prototype.send = function () {
    var self = this;
    var q = String(this.input.value || "").trim();
    if (!q || this.busy) return;
    if (!this.urls.ask) { this.notice("unconfigured", "This page has no agent bridge.", "The app did not set data-ask."); return; }
    this.input.value = "";
    this.bubble("you", q);
    var prior = this.thread.slice(-6);
    // What is on screen, read NOW: the moment of asking is what the question is about.
    var readPage = pageOn();
    if (readPage) {
      try { var seen = pageContext(this.panel); if (seen) prior = prior.concat([{ role: "page", text: seen }]); else readPage = false; }
      catch (e) { readPage = false; }
    }
    this.thread.push({ role: "you", text: q });
    this.busy = true;
    this.sendBtn.disabled = true;
    var typing = el("div", { class: "hub-agent-typing", "aria-label": "The agent is answering" }, [el("i"), el("i"), el("i")]);
    this.log.appendChild(typing);
    this.log.scrollTop = this.log.scrollHeight;
    function done() { self.busy = false; self.sendBtn.disabled = false; if (typing.parentNode) typing.parentNode.removeChild(typing); }
    postJSON(this.urls.ask, { question: q, thread: prior, conversation_id: this.conversationId || undefined })
      .then(function (raw) {
        done();
        var b = unwrap(raw) || {};
        if (b.ok) {
          var cites = (b.citations || []).length ? el("ul", { class: "hub-agent-cites" }, b.citations.map(function (c) {
            return el("li", null, [c.href ? el("a", { href: c.href, target: "_blank", rel: "noopener", text: c.label })
                                          : el("span", { text: c.label })]);
          })) : null;
          var said = self.bubble("agent", b.answer, cites);
          // An answer to a question that carried the page says so under it.
          if (readPage) said.appendChild(el("span", { class: "hub-agent-meta", text: "read this page" }));
          self.thread.push({ role: "agent", text: b.answer });
          if (b.conversation_id) { self.conversationId = b.conversation_id; store(self.key + "conversation", b.conversation_id); }
          if (b.history === "unconfigured") self.notice("info", "Past chats are off.", "This answer was not saved to your history.");
        } else if (b.reason === "unconfigured") {
          self.notice("unconfigured", "The agent is not connected yet.", b.error || "");
        } else {
          self.notice("failed", "That question did not get an answer.", b.error || (raw && raw.errors ? JSON.stringify(raw.errors) : "Unknown failure."),
            function () { self.retry(q); });
        }
      })
      .catch(function (err) {
        done();
        self.notice("failed", "The app's bridge could not be reached.", String(err && err.message || err),
          function () { self.retry(q); });
      });
  };
  /* A retry re-asks the SAME question: its unanswered bubble is removed first, so the log does
     not show the question twice with one answer. */
  Agent.prototype.retry = function (q) {
    var mine = this.log.querySelectorAll(".hub-agent-msg.is-you");
    var last = mine[mine.length - 1];
    if (last && last.parentNode) last.parentNode.removeChild(last);
    if (this.thread.length && this.thread[this.thread.length - 1].role === "you") this.thread.pop();
    this.input.value = q;
    this.send();
  };

  /* ---- past chats ---- */
  Agent.prototype.togglePast = function () {
    var show = this.past.hidden;
    this.past.hidden = !show;
    this.pastBtn.setAttribute("aria-pressed", show ? "true" : "false");
    if (show) this.loadPast(store(this.key + "scope") || "app");
  };
  Agent.prototype.loadPast = function (scope) {
    var self = this;
    store(this.key + "scope", scope);
    this.past.textContent = "";
    var seg = el("div", { class: "hub-agent-seg", role: "group", "aria-label": "Which conversations" }, [
      el("button", { type: "button", "aria-pressed": scope === "app" ? "true" : "false", text: "This app",
        onclick: function () { self.loadPast("app"); } }),
      el("button", { type: "button", "aria-pressed": scope === "all" ? "true" : "false", text: "All apps",
        onclick: function () { self.loadPast("all"); } })
    ]);
    var list = el("div", { class: "hub-agent-past-list" }, [el("p", { class: "hub-agent-muted", text: "Loading…" })]);
    this.past.appendChild(seg);
    this.past.appendChild(list);
    if (!this.urls.history) { list.textContent = ""; list.appendChild(el("p", { class: "hub-agent-muted", text: "This app has not wired past chats." })); return; }
    var sep = this.urls.history.indexOf("?") >= 0 ? "&" : "?";
    getJSON(this.urls.history + sep + "scope=" + scope).then(function (raw) {
      var b = unwrap(raw) || {};
      list.textContent = "";
      if (!b.ok) {
        list.appendChild(el("p", { class: "hub-agent-muted", text: (b.reason === "unconfigured" ? "Past chats are off: " : "Could not load past chats: ") + (b.error || "") }));
        return;
      }
      var rows = b.conversations || [];
      if (!rows.length) {
        list.appendChild(el("p", { class: "hub-agent-muted", text: scope === "app" ? "No past chats from this app yet." : "No past chats yet." }));
        return;
      }
      rows.forEach(function (c) {
        list.appendChild(el("button", { class: "hub-agent-past-row" + (c.id === self.conversationId ? " is-current" : ""), type: "button",
          onclick: function () { self.loadConversation(c.id); self.togglePast(); } }, [
          el("span", { class: "hub-agent-past-title", text: c.title || "Untitled conversation" }),
          el("span", { class: "hub-agent-muted", text: [c.app, c.updated_at ? String(c.updated_at).slice(0, 10) : ""].filter(Boolean).join(" · ") })
        ]));
      });
      if (b.truncated) list.appendChild(el("p", { class: "hub-agent-muted", text: "Showing the most recent 50." }));
    }).catch(function (err) { list.textContent = ""; list.appendChild(el("p", { class: "hub-agent-muted", text: "Could not load past chats: " + err })); });
  };
  Agent.prototype.loadConversation = function (id, quiet) {
    var self = this;
    if (!this.urls.conversation || !id) return;
    var sep = this.urls.conversation.indexOf("?") >= 0 ? "&" : "?";
    getJSON(this.urls.conversation + sep + "id=" + encodeURIComponent(id)).then(function (raw) {
      var b = unwrap(raw) || {};
      if (!b.ok) {
        if (quiet) { self.conversationId = ""; store(self.key + "conversation", null); return; }
        self.notice("failed", "That conversation could not be opened.", b.error || "");
        return;
      }
      var turns = (b.conversation || {}).turns || [];
      self.thread = [];
      self.log.textContent = "";
      self.conversationId = id;
      store(self.key + "conversation", id);
      turns.forEach(function (t) {
        var role = t.role === "you" ? "you" : "agent";
        self.bubble(role, t.text);
        self.thread.push({ role: role, text: t.text });
      });
      if (!turns.length) self.renderEmpty();
    }).catch(function () { /* the empty state stands */ });
  };

  function boot() {
    Array.prototype.forEach.call(doc.querySelectorAll("[data-hub-agent]"), function (m) {
      if (!m._hubAgent) m._hubAgent = new Agent(m);
    });
  }
  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", boot); else boot();
})();
