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
    this.form = el("div", { class: "hub-agent-compose" }, [this.input, this.sendBtn]);
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
    this.renderEmpty();
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
      // The edge tab steps aside for the open panel; the float stays, stepped left of it.
      b.classList.toggle("is-hidden", open && b.classList.contains("is-sidebar"));
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
  };
  Agent.prototype.open = function (quiet) {
    this.isOpen = true;
    this.panel.hidden = false;
    store(this.key + "open", "1");
    this.dock();
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
          self.bubble("agent", b.answer, cites);
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
