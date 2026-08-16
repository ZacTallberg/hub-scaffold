/*
 * hub.js — hub client RENDERER (shared kit)
 * --------------------------------------------------
 * Renders the entire hub UI from the canonical <script id="hub-data"> JSON island (the SAME
 * payload /hub.json serves — UI == API by construction), then keeps it LIVE: an SSE cursor tells
 * the page that something moved, and the page re-reads the canonical board to find out what.
 * Animation is never allowed to become a second source of truth.
 *
 * The board is a COCKPIT, not a table dump. What an operator needs to see without asking:
 * who is working right now and on what step, what needs a human, how fast the fleet is draining
 * the queue, whether the board is still being kept current, and what the dependency graph says
 * the floor on finishing actually is.
 *
 * Contract: shell.css owns the look; palette.js owns Cmd-K. This file is pure DOM (textContent —
 * never innerHTML of snapshot text). Zero deps, zero CDN.
 */
(function (global) {
  "use strict";
  var doc = document;
  var win = global;

  /* ---- inline icon set (24x24 stroke; static trusted markup) ---- */
  var P = {
    gauge: '<path d="M12 14a2 2 0 100-4 2 2 0 000 4z"/><path d="M13.4 10.6l3.6-3.6"/><path d="M5 18a8 8 0 1114 0"/>',
    checks: '<path d="M3 7l3 3 5-5"/><path d="M3 16l3 3 5-5"/><path d="M13 6h8"/><path d="M13 15h8"/>',
    branch: '<circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="8" r="2.5"/><path d="M6 8.5v7"/><path d="M18 10.5c0 4-6 1.5-6 5"/>',
    package: '<path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>',
    warning: '<path d="M12 3l9 16H3l9-16z"/><path d="M12 10v4"/><path d="M12 17.5v.5"/>',
    stack: '<path d="M12 3l9 5-9 5-9-5 9-5z"/><path d="M3 13l9 5 9-5"/>',
    rocket: '<path d="M5 15c-2 1-2 5-2 5s4 0 5-2"/><path d="M9 15l-3-3c2-7 7-9 12-9 0 5-2 10-9 12z"/><circle cx="14.5" cy="9.5" r="1.5"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
    close: '<path d="M6 6l12 12M18 6L6 18"/>',
    refresh: '<path d="M21 12a9 9 0 11-3-6.7L21 8"/><path d="M21 4v4h-4"/>',
    check: '<circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-6"/>',
    xc: '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8v.5"/>',
    tray: '<path d="M4 14l2 4h12l2-4"/><path d="M4 14V5a1 1 0 011-1h14a1 1 0 011 1v9"/>',
    cube: '<path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>',
    bolt: '<path d="M13 2L4 14h6l-1 8 9-12h-6l1-8z"/>',
    pulse: '<path d="M3 12h4l2-6 4 12 2-6h6"/>',
    users: '<circle cx="9" cy="8" r="3.2"/><path d="M2.5 19a6.5 6.5 0 0113 0"/><path d="M16 5.2a3.2 3.2 0 010 5.6"/><path d="M18 13.5a6.5 6.5 0 013.5 5.5"/>',
    target: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1"/>',
    route: '<circle cx="6" cy="19" r="2.5"/><circle cx="18" cy="5" r="2.5"/><path d="M8.5 19H14a4 4 0 000-8H10a4 4 0 010-8h5.5"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>'
  };
  function icon(name, cls) {
    var s = doc.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("fill", "none");
    s.setAttribute("stroke", "currentColor");
    s.setAttribute("stroke-width", "2");
    s.setAttribute("stroke-linecap", "round");
    s.setAttribute("stroke-linejoin", "round");
    if (cls) s.setAttribute("class", cls);
    s.setAttribute("aria-hidden", "true");
    s.setAttribute("focusable", "false");
    s.innerHTML = P[name] || P.info; // static trusted markup only — never snapshot text
    return s;
  }

  /* ---- DOM helper (safe: text via textContent) ---- */
  function el(tag, attrs, kids) {
    var n = doc.createElement(tag);
    if (attrs) for (var k in attrs) {
      if (k === "text") n.textContent = attrs[k];
      else if (k === "class") n.className = attrs[k];
      else if (k === "html") n.innerHTML = attrs[k]; // ONLY for trusted static (icons)
      else if (k.slice(0, 2) === "on" && typeof attrs[k] === "function") n.addEventListener(k.slice(2), attrs[k]);
      else if (attrs[k] != null) n.setAttribute(k, attrs[k]);
    }
    if (kids != null) (Array.isArray(kids) ? kids : [kids]).forEach(function (c) {
      if (c == null) return;
      n.appendChild(typeof c === "string" ? doc.createTextNode(c) : c);
    });
    return n;
  }
  function svgEl(tag, attrs, kids) {
    var n = doc.createElementNS("http://www.w3.org/2000/svg", tag);
    if (attrs) for (var k in attrs) { if (attrs[k] != null) n.setAttribute(k, attrs[k]); }
    if (kids != null) (Array.isArray(kids) ? kids : [kids]).forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }

  /* ---- status role vocabulary (mirrors the server _SROLE) ---- */
  var GLYPH = { pass: "✓", warn: "▲", fail: "✕", info: "•", stale: "◌" };
  var SROLE = {
    task: { done: "pass", in_progress: "info", blocked: "warn", todo: "stale", dropped: "stale", shadow: "warn" },
    adr: { accepted: "pass", proposed: "info", superseded: "stale", deprecated: "warn", rejected: "fail" },
    feat: { shipped: "pass", partial: "warn", planned: "info", experimental: "info", removed: "stale" },
    gap: { open: "fail", investigating: "warn", mitigated: "info", closed: "pass", "wont-fix": "stale" },
    cap: { extracted: "pass", reusable: "pass", proven: "pass", prototype: "warn", concept: "info", service: "info" }
  };
  function roleOf(type, status) { return (SROLE[type] || {})[status] || "info"; }
  function badge(type, status) {
    if (!status) return doc.createTextNode("");
    var r = roleOf(type, status);
    return el("span", { class: "badge b-" + r, title: status }, [
      el("span", { class: "b-glyph", "aria-hidden": "true", text: GLYPH[r] }),
      doc.createTextNode(" " + status)
    ]);
  }
  function localId(id) { id = String(id == null ? "" : id); var i = id.lastIndexOf(":"); return i >= 0 ? id.slice(i + 1) : id; }

  /* ---- formatters ---- */
  function fmtAge(s) {
    if (s == null) return "";
    if (s < 60) return Math.max(0, Math.round(s)) + "s";
    if (s < 3600) return Math.floor(s / 60) + "m";
    if (s < 86400) return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
    return Math.floor(s / 86400) + "d " + Math.floor((s % 86400) / 3600) + "h";
  }
  function fmtInt(n) { return String(n == null ? 0 : n).replace(/\B(?=(\d{3})+(?!\d))/g, ","); }
  function relativeTime(value) {
    if (!value) return "";
    var t = Date.parse(value);
    if (isNaN(t)) return "";
    var s = Math.round((Date.now() - t) / 1000);
    if (s < 5) return "just now";
    if (s < 0) return "in " + fmtAge(-s);
    return fmtAge(s) + " ago";
  }

  /* ---- data ---- */
  function parseData() {
    var e = doc.getElementById("hub-data");
    try { return JSON.parse(e.textContent || "{}"); } catch (x) { return {}; }
  }
  var D = parseData();
  var BY_ID = {};
  var COLLECTIONS = ["tasks", "adrs", "feats", "gaps", "caps", "deploys", "notes"];
  function rebuildIndex() {
    BY_ID = {};
    COLLECTIONS.forEach(function (k) {
      (D[k] || []).forEach(function (r) { if (r && r.id) BY_ID[r.id] = r; });
    });
  }
  rebuildIndex();
  function live() { return D.live || {}; }

  /* ============================ TAB DEFINITIONS ============================ */
  var TABS = [
    { key: "overview", label: "Overview", icon: "gauge", build: buildOverview },
    { key: "tasks", label: "Tasks", icon: "checks", pick: function (d) { return d.tasks || []; }, type: "task", cols: COLS_TASK(), build: buildTaskTab },
    { key: "adrs", label: "ADRs", icon: "branch", pick: function (d) { return d.adrs || []; }, type: "adr", cols: COLS_ADR() },
    { key: "feats", label: "Features", icon: "package", pick: function (d) { return d.feats || []; }, type: "feat", cols: COLS_FEAT() },
    { key: "gaps", label: "Gaps", icon: "warning", pick: function (d) { return d.gaps || []; }, type: "gap", cols: COLS_GAP() },
    { key: "caps", label: "Capabilities", icon: "stack", pick: function (d) { return d.caps || []; }, type: "cap", cols: COLS_CAP() },
    { key: "deploys", label: "Deploys", icon: "rocket", pick: function (d) { return d.deploys || []; }, type: "deploy", cols: COLS_DEPLOY() },
    { key: "notes", label: "Findings", icon: "stack", pick: function (d) { return d.notes || []; }, type: "note", cols: COLS_NOTE() }
  ];
  TABS.forEach(function (t) { if (t.pick) t.rows = t.pick(D); });
  function tabByKey(key) { for (var i = 0; i < TABS.length; i++) if (TABS[i].key === key) return TABS[i]; return null; }

  /* ---- live per-task decorations ---- */
  function leaseOf(taskId) {
    var rows = live().inflight || [];
    for (var i = 0; i < rows.length; i++) if (rows[i].task === taskId) return rows[i];
    return null;
  }
  function receiptOf(task) {
    var runs = (task && task.verification_run) || [];
    if (!Array.isArray(runs)) runs = [runs];
    for (var i = runs.length - 1; i >= 0; i--) if (runs[i] && runs[i].exit_code === 0) return runs[i];
    return runs.length ? runs[runs.length - 1] : null;
  }
  function taskProgress(task) {
    var plan = (task && task.plan) || [];
    if (!plan.length) return null;
    var done = plan.filter(function (s) { return s && s.done; }).length;
    return { done: done, total: plan.length, pct: Math.round(done * 100 / plan.length),
             step: (plan.filter(function (s) { return s && !s.done; })[0] || {}).step || null };
  }
  function taskStatusBadge(task) {
    // A done task says HOW it was granted. Under the receipt gate `done` means an exit-0
    // verification_run was submitted with it — a done row with no receipt behind it is a
    // different claim, and the badge must not render them identically.
    var b = badge("task", task.status);
    if (task.status !== "done") return b;
    var r = receiptOf(task);
    var proven = !!(r && r.exit_code === 0);
    b.setAttribute("title", proven ? ("granted by receipt: " + (r.command || "") + " → exit 0")
                                   : "done recorded WITHOUT a passing receipt");
    if (!proven) b.className = "badge b-warn";
    return b;
  }

  /* column descriptors: {label, k (sort key), cell(rec)->node, cls} */
  function txt(s, cls) { return el("td", cls ? { class: cls } : null, [doc.createTextNode(s == null ? "" : String(s))]); }
  function COLS_TASK() {
    return [
      { label: "ID", k: "legacy_ref", cls: "col-id", cell: function (r) { return txt(r.legacy_ref || localId(r.id), "col-id"); } },
      { label: "Status", k: "status", cls: "col-status", cell: function (r) { return el("td", { class: "col-status" }, [taskStatusBadge(r)]); } },
      { label: "Held by", k: "id", cls: "col-pickup", sortVal: function (r) { return leaseOf(r.id) ? 0 : 1; }, cell: function (r) {
          var lease = leaseOf(r.id);
          if (!lease) return txt("—", "cell-sub col-pickup");
          return el("td", { class: "col-pickup" }, [el("span", { class: "lease-chip" + (lease.stalled ? " is-stalled" : ""),
            title: lease.agent + " has held this " + fmtAge(lease.age_s) }, [
            el("span", { class: "lease-dot", "aria-hidden": "true" }),
            doc.createTextNode(lease.agent || "worker")
          ])]);
        } },
      { label: "Phase", k: "phase", cls: "col-phase", cell: function (r) { return txt(r.phase, "cell-sub col-phase"); } },
      { label: "Priority", k: "priority", cls: "col-priority", cell: function (r) { return el("td", { class: "col-priority" }, [
          el("span", { class: "priority priority-" + (r.priority || "P3"), text: r.priority || "—" })]); } },
      { label: "Plan", k: "plan", cls: "col-progress", sortVal: function (r) { var p = taskProgress(r); return p ? p.pct : -1; }, cell: function (r) {
          var p = taskProgress(r);
          if (!p) return txt("unplanned", "cell-sub col-progress");
          return el("td", { class: "col-progress task-progress-cell" }, [
            el("span", { class: "mini-progress", "aria-hidden": "true" }, [el("span", { style: "width:" + p.pct + "%" })]),
            el("span", { class: "mini-progress-label", text: p.done + "/" + p.total })]);
        } },
      { label: "Title", k: "title", cls: "col-title", cell: function (r) { return txt(r.title, "col-title"); } }
    ];
  }
  function COLS_ADR() {
    return [
      { label: "#", k: "number", cls: "col-id", cell: function (r) { return txt(String(r.number).padStart ? String(r.number).padStart(4, "0") : r.number, "col-id"); } },
      { label: "Status", k: "status", cell: function (r) { return el("td", null, [badge("adr", r.status)]); } },
      { label: "Title", k: "title", cls: "col-title", cell: function (r) { return txt(r.title, "col-title"); } }
    ];
  }
  function COLS_FEAT() {
    return [
      { label: "Status", k: "status", cell: function (r) { return el("td", null, [badge("feat", r.status)]); } },
      { label: "Feature", k: "name", cls: "col-title", cell: function (r) { return txt(r.name, "col-title"); } },
      { label: "Summary", k: "summary", cell: function (r) { return txt(r.summary, "cell-sub"); } },
      { label: "Tasks", k: "tasks", cls: "num", cell: function (r) { return txt((r.tasks || []).length, "num"); } }
    ];
  }
  function COLS_GAP() {
    var order = { P0: 0, P1: 1, P2: 2, P3: 3 };
    return [
      { label: "Sev", k: "severity", sortVal: function (r) { return order[r.severity] == null ? 9 : order[r.severity]; },
        cell: function (r) { return el("td", null, [el("span", { class: "sev-badge sev-" + (r.severity || "P3"), text: r.severity || "—" })]); } },
      { label: "Status", k: "status", cell: function (r) { return el("td", null, [badge("gap", r.status)]); } },
      { label: "Title", k: "title", cls: "col-title", cell: function (r) { return txt(r.title, "col-title"); } },
      { label: "Source", k: "source", cell: function (r) { return txt(r.source, "cell-sub"); } }
    ];
  }
  function COLS_CAP() {
    return [
      { label: "Maturity", k: "maturity", cell: function (r) { return el("td", null, [badge("cap", r.maturity)]); } },
      { label: "Capability", k: "name", cls: "col-title", cell: function (r) { return txt(r.name, "col-title"); } },
      { label: "Needs", k: "needs", cell: function (r) { return txt(r.needs, "cell-sub"); } }
    ];
  }
  function COLS_NOTE() {
    return [
      { label: "Category", k: "category", cell: function (r) { return txt(r.category || "—", "cell-sub"); } },
      { label: "Finding", k: "title", cls: "col-title", cell: function (r) { return txt(r.title, "col-title"); } },
      { label: "Tags", k: "tags", cell: function (r) { return txt((r.tags || []).join(", "), "cell-sub"); } }
    ];
  }
  function deployCoherence(r) {
    var ok = !!r.audit_ok;
    return el("span", { class: "badge b-" + (ok ? "pass" : "fail") }, [
      el("span", { class: "b-glyph", "aria-hidden": "true", text: ok ? GLYPH.pass : GLYPH.fail }),
      doc.createTextNode(ok ? " ok" : " not ok")
    ]);
  }
  function COLS_DEPLOY() {
    return [
      { label: "At", k: "at", cls: "col-id", cell: function (r) { return txt(r.at, "col-id"); } },
      { label: "Build", k: "build", cell: function (r) { return txt(r.build); } },
      { label: "SHA", k: "sha", cls: "col-id", cell: function (r) { return txt(r.sha, "col-id"); } },
      { label: "Audit", k: "audit_ok", cell: function (r) { return el("td", null, [deployCoherence(r)]); } }
    ];
  }

  /* ============================ FACETS ============================ */
  // One-click narrowing on the field that actually distinguishes a type's rows. A free-text box
  // makes the operator guess the vocabulary; a facet bar SHOWS it, with counts.
  var FACET_FIELD = { tasks: "status", adrs: "status", feats: "status", gaps: "severity",
                      caps: "maturity", deploys: "audit_ok", notes: "category" };
  function facetField(tab) { return FACET_FIELD[tab.key]; }
  function facetCounts(tab, field) {
    var counts = {};
    (tab.rows || []).forEach(function (r) {
      var v = r[field];
      if (v === true) v = "ok"; else if (v === false) v = "not ok";
      v = (v == null || v === "") ? "—" : String(v);
      counts[v] = (counts[v] || 0) + 1;
    });
    return counts;
  }
  function setFacet(tabKey, value) {
    var tab = tabByKey(tabKey);
    if (!tab) return;
    tab._facet = (tab._facet === value) ? null : value;
    renderFacetBar(tab);
    renderRows(tab);
    if (tab.key === "tasks") renderTaskStage(tab, {});
  }
  function renderFacetBar(tab) {
    if (!tab._facetBar) return;
    var field = facetField(tab);
    var counts = facetCounts(tab, field);
    tab._facetBar.textContent = "";
    var keys = Object.keys(counts).sort();
    tab._facetBar.classList.toggle("is-empty", keys.length < 2);
    if (keys.length < 2) return;                      // a single-value facet narrows nothing
    tab._facetBar.appendChild(el("span", { class: "facet-label", text: field }));
    keys.forEach(function (v) {
      var on = tab._facet === v;
      var chip = el("button", { class: "facet-chip" + (on ? " is-on" : ""), type: "button",
        "aria-pressed": on ? "true" : "false" }, [
        doc.createTextNode(v + " "), el("span", { class: "facet-n", text: String(counts[v]) })
      ]);
      chip.addEventListener("click", function () { setFacet(tab.key, v); });
      tab._facetBar.appendChild(chip);
    });
    if (tab._facet) {
      var clear = el("button", { class: "facet-chip is-clear", type: "button", text: "clear" });
      clear.addEventListener("click", function () { setFacet(tab.key, tab._facet); });
      tab._facetBar.appendChild(clear);
    }
  }
  function facetMatch(tab, r) {
    if (!tab._facet) return true;
    var v = r[facetField(tab)];
    if (v === true) v = "ok"; else if (v === false) v = "not ok";
    return String((v == null || v === "") ? "—" : v) === tab._facet;
  }

  /* ============================ TABLE RENDER ============================ */
  function buildTableTab(tab) {
    var pane = el("div", { class: "tab-content", id: "tab-" + tab.key, role: "tabpanel",
      "aria-labelledby": "tab-btn-" + tab.key, tabindex: "0" });
    var search = el("input", { type: "search", placeholder: "Filter " + tab.label.toLowerCase() + "…", "aria-label": "Filter " + tab.label });
    var countEl = el("span", { class: "stat-value", role: "status", "aria-live": "polite", text: String(tab.rows.length) });
    var toolbar = el("div", { class: "toolbar" }, [
      el("div", { class: "search-box" }, [icon("search", "s-icon"), search]),
      el("div", { class: "toolbar-spacer" }),
      el("div", { class: "stats-bar" }, [el("div", { class: "stat-item" }, [countEl, doc.createTextNode(" " + tab.label.toLowerCase())])])
    ]);
    var facetBar = el("div", { class: "facet-bar" });
    var thead = el("tr");
    tab.cols.forEach(function (c, i) {
      var sortButton = el("button", { class: "sort-btn", type: "button" }, [
        doc.createTextNode(c.label + " "), el("span", { class: "sort-ind", "aria-hidden": "true", text: "↕" })
      ]);
      var th = el("th", { scope: "col", "aria-sort": "none",
        class: ((c.cls || "") + (c.cls && c.cls.indexOf("num") >= 0 ? " num" : "") + " sortable").trim() }, [sortButton]);
      sortButton.addEventListener("click", function () { sortBy(tab, i); });
      thead.appendChild(th);
    });
    var tbody = el("tbody");
    var table = el("table", { class: "data-table" + (tab.key === "tasks" ? " task-table" : "") }, [
      el("caption", { class: "sr-only", text: tab.label + " on the canonical Hub board" }),
      el("thead", null, [thead]), tbody]);
    var wrap = el("div", { class: "table-wrapper" + (tab.key === "tasks" ? " task-table-wrapper" : "") }, [table]);
    var stage = tab.key === "tasks" ? el("div", { class: "task-stage" }) : null;
    pane.append(toolbar, facetBar,
      el("div", { class: "content-area" }, [el("div", { class: "full-table-view" }, [stage, wrap].filter(Boolean))]));

    tab._tbody = tbody; tab._count = countEl; tab._thead = thead; tab._facetBar = facetBar; tab._stage = stage;
    renderFacetBar(tab);
    renderRows(tab);
    if (stage) renderTaskStage(tab, {});
    search.addEventListener("input", function () {
      tab._q = search.value.trim().toLowerCase();
      renderRows(tab);
      if (tab._stage) renderTaskStage(tab, {});
    });
    return pane;
  }
  function buildTaskTab(tab) { return buildTableTab(tab); }

  /* The LIVE STAGE: the handful of tasks a worker is holding right now, as cards with their plan
     step and lease age. A table row cannot carry a moving sub-progress bar legibly, and the tasks
     in flight are the ones an operator is actually watching. */
  function taskCard(task, lease) {
    var prog = taskProgress(task);
    var kids = [
      el("div", { class: "tcard-head" }, [
        el("span", { class: "tcard-id", text: task.legacy_ref || localId(task.id) }),
        taskStatusBadge(task),
        lease ? el("span", { class: "tcard-agent" + (lease.stalled ? " is-stalled" : ""),
                             title: "held " + fmtAge(lease.age_s) }, [
          el("span", { class: "lease-dot", "aria-hidden": "true" }),
          doc.createTextNode(lease.agent || "worker")
        ]) : null
      ].filter(Boolean)),
      el("div", { class: "tcard-title", text: task.title || localId(task.id) })
    ];
    if (prog) {
      kids.push(el("div", { class: "tcard-prog" }, [
        el("div", { class: "tcard-track" }, [el("div", { class: "tcard-fill", style: "width:" + prog.pct + "%" })]),
        el("div", { class: "tcard-steps" }, [
          el("span", { text: "step " + prog.done + "/" + prog.total }),
          prog.step ? el("span", { class: "tcard-step", text: prog.step }) : null
        ].filter(Boolean))
      ]));
    } else if (lease) {
      kids.push(el("div", { class: "tcard-steps is-noplan", text: "no plan recorded — progress is invisible until the worker writes one" }));
    }
    if (lease && lease.stalled) {
      kids.push(el("div", { class: "tcard-alert", text: "stalled: held " + fmtAge(lease.age_s) + " without finishing" }));
    }
    var card = el("button", { class: "tcard" + (lease && lease.stalled ? " is-stalled" : "") + (lease ? " is-live" : ""),
      type: "button", "data-task-id": task.id, "aria-label": (task.title || localId(task.id)) }, kids);
    card.addEventListener("click", function () { openEntity("task", task); });
    return card;
  }
  function renderTaskStage(tab, changed) {
    var stage = tab && tab._stage;
    if (!stage) return;
    stage.textContent = "";
    var inflight = live().inflight || [];
    if (!inflight.length) return;
    stage.appendChild(el("div", { class: "stage-head" }, [
      icon("pulse"), doc.createTextNode(" In flight now · " + inflight.length)
    ]));
    var grid = el("div", { class: "stage-grid" });
    inflight.forEach(function (lease) {
      var task = BY_ID[lease.task];
      if (task) grid.appendChild(taskCard(task, lease));
    });
    stage.appendChild(grid);
  }

  function renderRows(tab) {
    var tb = tab._tbody; if (!tb) return;
    tb.textContent = "";
    var rows = tab.rows.slice();
    if (tab._sortIdx != null) {
      var c = tab.cols[tab._sortIdx], dir = tab._sortDir;
      rows.sort(function (a, b) {
        var x = c.sortVal ? c.sortVal(a) : (a[c.k] == null ? "" : a[c.k]);
        var y = c.sortVal ? c.sortVal(b) : (b[c.k] == null ? "" : b[c.k]);
        if (Array.isArray(x)) x = x.length; if (Array.isArray(y)) y = y.length;
        var cmp = (typeof x === "number" && typeof y === "number") ? x - y : String(x).localeCompare(String(y));
        return dir === "desc" ? -cmp : cmp;
      });
    }
    var q = tab._q, shown = 0;
    rows.forEach(function (r) {
      if (!facetMatch(tab, r)) return;
      if (q) {
        var hay = [r.legacy_ref, r.title, r.name, r.summary, r.status, r.severity, r.maturity, r.phase, r.source, r.build, r.sha, localId(r.id)].join(" ").toLowerCase();
        if (hay.indexOf(q) < 0) return;
      }
      shown++;
      var tr = el("tr", { id: tab.type + "-" + localId(r.id), tabindex: "0", "data-hub-row": "",
        "data-entity-id": r.id, role: "button", "aria-label": (r.title || r.name || localId(r.id)) });
      tab.cols.forEach(function (c) {
        var cell = c.cell(r);
        cell.setAttribute("data-label", c.label);
        if (c.cls) c.cls.split(/\s+/).forEach(function (name) { if (name) cell.classList.add(name); });
        tr.appendChild(cell);
      });
      tr.addEventListener("click", function () { openEntity(tab.type, r); });
      tr.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openEntity(tab.type, r); } });
      tr.addEventListener("focus", function () { activate(tab.key); });
      tb.appendChild(tr);
    });
    if (!shown) {
      tb.appendChild(el("tr", null, [el("td", { colspan: tab.cols.length }, [
        el("div", { class: "empty-state" }, [icon("tray"), el("p", { text: (q || tab._facet) ? "No " + tab.label.toLowerCase() + " match the current filter — clear it to see all." : "No " + tab.label.toLowerCase() + " yet." })])
      ])]));
    }
    if (tab._count) tab._count.textContent = String(shown);
  }

  function updateSortHeaders(tab) {
    if (!tab._thead) return;
    [].forEach.call(tab._thead.children, function (th, i) {
      var on = i === tab._sortIdx;
      th.classList.toggle("sort-asc", on && tab._sortDir === "asc");
      th.classList.toggle("sort-desc", on && tab._sortDir === "desc");
      var ind = th.querySelector(".sort-ind");
      if (ind) ind.textContent = on ? (tab._sortDir === "asc" ? "↑" : "↓") : "↕";
      th.setAttribute("aria-sort", on ? (tab._sortDir === "asc" ? "ascending" : "descending") : "none");
    });
  }
  function sortBy(tab, idx) {
    if (tab._sortIdx === idx) tab._sortDir = tab._sortDir === "asc" ? "desc" : "asc";
    else { tab._sortIdx = idx; tab._sortDir = "asc"; }
    updateSortHeaders(tab);
    renderRows(tab);
  }

  /* ============================ COCKPIT ============================ */
  function eventLabel(event) {
    var labels = {
      "task.created": "Task entered the system",
      "task.updated": "Task progress changed",
      "task.transitioned": "Task completed",
      "decision.logged": "Decision recorded",
      "deploy.created": "Release recorded",
      "adr.upserted": "Architecture decision changed",
      "gap.created": "Gap surfaced",
      "cap.upserted": "Capability changed"
    };
    return labels[event.event] || String(event.event || "Event").replace(/[._]/g, " ");
  }

  var ATTN_LABEL = {
    "board-drained": "Board drained", "stalled-lease": "Stalled worker",
    "dangling-dep": "Unsatisfiable dep", "governance-amber": "Needs a ruling",
    "blocked": "Blocked", "needs-spec": "Needs spec", "circuit-open": "Circuit open",
    "adherence-drift": "Board drifting", "unlanded": "Not landed",
    "delivery-unmeasured-landing": "Landing unknown",
    "delivery-unmeasured-release": "Release unknown",
    "delivery-unmeasured-live": "Live state unknown"
  };
  var ATTN_TONE = {
    "board-drained": "fail", "stalled-lease": "warn", "dangling-dep": "warn",
    "governance-amber": "warn", "blocked": "info", "needs-spec": "info",
    "circuit-open": "fail", "adherence-drift": "warn", "unlanded": "warn",
    "delivery-unmeasured-landing": "warn", "delivery-unmeasured-release": "warn",
    "delivery-unmeasured-live": "warn"
  };
  function attentionItem(it) {
    var tone = ATTN_TONE[it.kind] || "info";
    var node = el("button", { class: "attn-item t-" + tone, type: "button", "data-focus-key": "attention:" + (it.id || it.kind),
      "aria-label": (ATTN_LABEL[it.kind] || it.kind) + ": " + (it.title || it.reason) }, [
      el("span", { class: "attn-kind b-" + tone, text: ATTN_LABEL[it.kind] || it.kind }),
      el("span", { class: "attn-body" }, [
        it.title ? el("span", { class: "attn-title", text: it.title }) : null,
        el("span", { class: "attn-reason", text: it.reason })
      ].filter(Boolean))
    ]);
    if (it.id && BY_ID[it.id]) {
      node.addEventListener("click", function () { openEntity(BY_ID[it.id].type || "task", BY_ID[it.id]); });
    } else if (it.route && it.route.view === "audit") {
      node.addEventListener("click", function () { openAuditViolation(it.route.violation); });
    } else if (it.route && it.route.focus === "adherence") {
      node.addEventListener("click", function () { focusCard("adherenceCard"); });
    } else if (it.route && it.route.focus === "delivery") {
      node.addEventListener("click", function () { focusCard("deliveryCard"); });
    } else {
      node.disabled = true;
      node.title = "nothing to open for this item";
    }
    return node;
  }
  function focusCard(id) {
    try { activate("overview"); } catch (e) { /* stay put */ }
    win.setTimeout(function () {
      var t = doc.getElementById(id);
      if (!t) return;
      t.scrollIntoView({ behavior: "smooth", block: "center" });
      flashClass(t, "bumped", 1400);
    }, 60);
  }
  function openAuditViolation(vid) {
    // A row whose subject is a VIOLATION, not an entity: take the operator to the audit card and
    // highlight the one it named. A disabled button was the cockpit saying "someone must rule on
    // this" and then refusing to show what.
    try { activate("overview"); } catch (e) { /* stay put */ }
    win.setTimeout(function () {
      var target = null, callouts = doc.querySelectorAll(".card .callout");
      for (var i = 0; i < callouts.length; i++) {
        var strong = callouts[i].querySelector("strong");
        if (strong && vid && strong.textContent.indexOf(vid) === 0) { target = callouts[i]; break; }
      }
      if (!target) { toast("Violation " + vid + " is no longer in the audit", "info"); return; }
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      flashClass(target, "bumped", 1400);
    }, 60);
  }
  function attentionRail(items) {
    items = items || [];
    var body = el("div", { class: "attn-list" });
    if (items.length) items.forEach(function (it) { body.appendChild(attentionItem(it)); });
    else body.appendChild(el("div", { class: "attn-clear" }, [
      el("span", { class: "b-glyph", "aria-hidden": "true", text: GLYPH.pass }),
      doc.createTextNode(" Nothing needs the operator — the fleet is self-sequencing.")
    ]));
    return el("section", { class: "card attention-card", id: "attentionCard", "aria-labelledby": "attnTitle" }, [
      el("div", { class: "card-header" }, [
        el("div", { class: "card-title", id: "attnTitle" }, [icon("warning"),
          doc.createTextNode("Needs the operator" + (items.length ? "  ·  " + items.length : ""))])
      ]),
      body
    ]);
  }

  function activityItem(event, newest) {
    var copy = [
      el("span", { class: "activity-topline" }, [
        el("strong", { text: eventLabel(event) }),
        el("time", { class: "rel-time", datetime: event.ts || "", "data-ts": event.ts || "", text: relativeTime(event.ts) })
      ]),
      el("span", { class: "activity-title", text: event.title || event.aggregate || "Canonical Hub event" }),
      el("span", { class: "activity-meta", text: "event " + (event.seq || "—") + (event.agent ? " · " + event.agent : "") })
    ];
    if (event.receipt) {
      copy.push(el("span", { class: "activity-receipt", text:
        "verified: " + (event.receipt.command || "") + "  exit " + event.receipt.exit_code +
        (event.receipt.ran_by ? "  by " + event.receipt.ran_by : "") }));
    }
    var node = el("button", { class: "activity-item" + (newest ? " is-new" : ""), type: "button",
      "data-seq": String(event.seq || ""), "data-entity-id": event.aggregate || "",
      "aria-label": eventLabel(event) + ": " + (event.title || event.aggregate || "") }, [
      el("span", { class: "activity-rail", "aria-hidden": "true" }, [el("span", { class: "activity-node" })]),
      el("span", { class: "activity-copy" }, copy)
    ]);
    if (event.aggregate && BY_ID[event.aggregate]) {
      node.addEventListener("click", function () {
        var entity = BY_ID[event.aggregate];
        openEntity(event.entity_type || entity.type, entity);
      });
    } else { node.disabled = true; }
    return node;
  }

  function sparkline(vals, bucketS) {
    vals = vals || [];
    var max = Math.max.apply(null, [1].concat(vals));
    var label = bucketS ? (" per " + fmtAge(bucketS)) : "";
    return el("div", { class: "spark", "aria-hidden": "true" }, vals.map(function (v) {
      return el("span", { class: "spark-bar" + (v ? "" : " is-zero"),
        style: "height:" + Math.round(4 + (v / max) * 26) + "px", title: v + " completed" + label });
    }));
  }

  /* ---- ADHERENCE RING: is the board still being FOLLOWED and kept current? ----
     Six dimensions as arc segments around one ring, each sized equally and filled to its own
     ratio, with the composite in the middle. A dimension whose denominator was empty renders as
     a GHOST segment rather than a full one — "nothing to measure" and "everything passed" look
     nothing alike here, which is the entire point of the block. */
  var ADH_ORDER = ["specced", "proven", "evidenced", "fresh", "current", "moving"];
  function adherenceRing(a) {
    var R = 54, CX = 64, CY = 64, GAP = 5;
    var seg = 360 / ADH_ORDER.length;
    var g = svgEl("svg", { viewBox: "0 0 128 128", width: "128", height: "128", role: "img",
      "aria-label": a.score == null ? "Board adherence unmeasured" : ("Board adherence " + a.score + " percent") });
    function arc(from, to, cls, r) {
      var a0 = (from - 90) * Math.PI / 180, a1 = (to - 90) * Math.PI / 180;
      var large = (to - from) > 180 ? 1 : 0;
      var d = "M " + (CX + r * Math.cos(a0)).toFixed(2) + " " + (CY + r * Math.sin(a0)).toFixed(2) +
              " A " + r + " " + r + " 0 " + large + " 1 " +
              (CX + r * Math.cos(a1)).toFixed(2) + " " + (CY + r * Math.sin(a1)).toFixed(2);
      return svgEl("path", { d: d, class: cls, fill: "none", "stroke-linecap": "round" });
    }
    ADH_ORDER.forEach(function (name, i) {
      var d = (a.dimensions || {})[name] || {};
      var start = i * seg + GAP / 2, end = (i + 1) * seg - GAP / 2;
      g.appendChild(arc(start, end, "adh-track", R));
      if (d.pct == null) {
        g.appendChild(arc(start, end, "adh-ghost", R));
      } else {
        var tone = d.pct >= 90 ? "pass" : d.pct >= 70 ? "warn" : "fail";
        var fillEnd = start + (end - start) * (d.pct / 100);
        if (fillEnd > start + 0.4) g.appendChild(arc(start, fillEnd, "adh-fill t-" + tone, R));
      }
    });
    var centre = svgEl("text", { x: "64", y: "60", "text-anchor": "middle", class: "adh-score",
      "font-size": "27" });
    centre.textContent = a.score == null ? "—" : (a.score + "%");
    var sub = svgEl("text", { x: "64", y: "78", "text-anchor": "middle", class: "adh-sub", "font-size": "10" });
    sub.textContent = a.score == null ? "unmeasured" : "adherence";
    g.appendChild(centre); g.appendChild(sub);
    return el("div", { class: "adh-ring" }, [g]);
  }
  function adherenceCard(a) {
    a = a || {};
    var legend = el("div", { class: "adh-legend" });
    ADH_ORDER.forEach(function (name) {
      var d = (a.dimensions || {})[name] || {};
      var tone = d.pct == null ? "ghost" : d.pct >= 90 ? "pass" : d.pct >= 70 ? "warn" : "fail";
      var row = el("button", { class: "adh-row t-" + tone, type: "button",
        "data-focus-key": "adherence:" + name,
        title: (a.meaning || {})[name] || name,
        "aria-label": name + " " + (d.pct == null ? "unmeasured" : d.pct + "%") }, [
        el("span", { class: "adh-key" }, [el("span", { class: "adh-swatch", "aria-hidden": "true" }),
          doc.createTextNode(name)]),
        el("span", { class: "adh-val", text: d.pct == null ? "n/a" : (d.pct + "%") }),
        el("span", { class: "adh-den", text: d.total ? (d.ok + "/" + d.total) : "nothing to measure" })
      ]);
      var bad = (a.offenders || {})[name];
      if (bad && bad.length && BY_ID[bad[0].id]) {
        row.addEventListener("click", function () { openEntity("task", BY_ID[bad[0].id]); });
      } else { row.disabled = true; }
      legend.appendChild(row);
    });
    var notes = [];
    if ((a.unmeasurable || []).length) {
      notes.push(el("p", { class: "cell-sub", text:
        "unmeasurable here: " + a.unmeasurable.join(", ") + " — an empty denominator is reported as n/a, never as 100%." }));
    }
    if (a.weakest) {
      notes.push(el("p", { class: "adh-weakest", text: "weakest: " + a.weakest + " — " + (a.weakest_meaning || "") }));
    }
    return el("section", { class: "card adherence-card", id: "adherenceCard", "aria-labelledby": "adhTitle" }, [
      el("div", { class: "card-header" }, [
        el("div", { class: "card-title", id: "adhTitle" }, [icon("target"), doc.createTextNode("Board adherence")]),
        el("span", { class: "badge b-" + (a.score == null ? "stale" : a.score >= 90 ? "pass" : a.score >= 70 ? "warn" : "fail"),
                     text: a.score == null ? "unmeasured" : (a.score + "%") })
      ]),
      el("div", { class: "card-body adh-body" }, [adherenceRing(a), legend].concat(notes))
    ]);
  }

  /* ---- DEPENDENCY DAG: the shape of what is left, not just its size ----
     Frontier layers as columns of nodes with the critical path threaded through them. The number
     "critical path 11" is a claim the operator has to take on faith; the chain shows WHICH
     eleven, so the one queue worth unblocking first is visible. */
  function dagChart(dg) {
    // The frontier, DRAWN: one column per dependency layer, the critical path threaded through
    // them as a flowing spine, and every layer labelled with how wide it actually is. The point
    // is that "critical path 11" becomes a shape you can read — where the board narrows to a
    // single file, and where it opens wide enough to absorb more workers.
    if (!dg || !dg.nodes) return null;
    var layers = dg.layers || [];
    if (!layers.length) return null;
    var CAP = 7;                                     // rows drawn per column before "+N"
    var n = layers.length;
    var tall = Math.min(CAP, Math.max.apply(null, layers));
    // Width tracks the number of layers and height the widest one, so the viewBox matches the
    // drawing rather than a fixed frame the drawing floats inside.
    var W = Math.max(210, Math.min(1200, n * 104));
    var H = Math.max(150, (tall - 1) * 19 + 96), MID = H / 2 - 14;
    var g = svgEl("svg", { viewBox: "0 0 " + W + " " + H, class: "dag-svg", role: "img",
      preserveAspectRatio: "xMidYMid meet",
      "aria-label": "Dependency frontier: " + n + " layers, widest " + dg.max_frontier_width
                    + ", critical path " + dg.critical_path_length });

    // gradient + glow so the spine reads as energy moving through the graph
    var defs = svgEl("defs");
    var lg = svgEl("linearGradient", { id: "dagSpine", x1: "0", y1: "0", x2: "1", y2: "0" });
    lg.appendChild(svgEl("stop", { offset: "0", "stop-color": "var(--accent, #4f7cff)" }));
    lg.appendChild(svgEl("stop", { offset: "1", "stop-color": "var(--pass, #2fae66)" }));
    defs.appendChild(lg);
    g.appendChild(defs);

    var stepX = W / (n + 1);
    var pts = layers.map(function (_w, i) { return stepX * (i + 1); });

    // the spine first, so nodes sit on top of it
    for (var i = 0; i < n - 1; i++) {
      var x1 = pts[i], x2 = pts[i + 1], mx = (x1 + x2) / 2;
      // a gentle S-curve rather than a straight rule — a dependency chain is a flow, and a
      // straight line between two dots reads as a divider
      var d = "M " + x1.toFixed(1) + " " + MID + " C " + mx.toFixed(1) + " " + MID + ", "
            + mx.toFixed(1) + " " + MID + ", " + x2.toFixed(1) + " " + MID;
      g.appendChild(svgEl("path", { d: d, class: "dag-edge on-path", fill: "none" }));
    }

    layers.forEach(function (width, i) {
      var x = pts[i];
      var shown = Math.min(width, CAP);
      // a soft column wash so a WIDE layer is visible as mass, not just as more dots
      if (width > 1) {
        g.appendChild(svgEl("rect", {
          x: (x - 17).toFixed(1), y: (MID - (shown - 1) / 2 * 19 - 15).toFixed(1),
          width: "34", height: ((shown - 1) * 19 + 30).toFixed(1),
          rx: "17", class: "dag-col" }));
      }
      for (var k = 0; k < shown; k++) {
        var y = MID + (k - (shown - 1) / 2) * 19;
        var onPath = k === 0;
        if (onPath) {
          g.appendChild(svgEl("circle", { cx: x.toFixed(1), cy: y.toFixed(1), r: "9",
            class: "dag-halo" }));
        }
        g.appendChild(svgEl("circle", { cx: x.toFixed(1), cy: y.toFixed(1), r: onPath ? "5.5" : "4",
          class: "dag-node" + (onPath ? " on-path" : "") }));
      }
      if (width > shown) {
        var more = svgEl("text", { x: x.toFixed(1), y: (MID + (shown - 1) / 2 * 19 + 26).toFixed(1),
          "text-anchor": "middle", class: "dag-more", "font-size": "10" });
        more.textContent = "+" + (width - shown);
        g.appendChild(more);
      }
      // per-layer width label along the base — the frontier's shape, in numbers
      var lbl = svgEl("text", { x: x.toFixed(1), y: (H - 8).toFixed(1), "text-anchor": "middle",
        class: "dag-axis", "font-size": "10" });
      lbl.textContent = String(width);
      g.appendChild(lbl);
    });

    var cap = svgEl("text", { x: (W / 2).toFixed(1), y: (H - 24).toFixed(1), "text-anchor": "middle",
      class: "dag-axis dag-axis-cap", "font-size": "9.5" });
    cap.textContent = "tasks workable at each step  →";
    g.appendChild(cap);
    return g;
  }
  function dagCard(dg) {
    if (!dg) return null;
    var chart = dagChart(dg);
    if (!dg.nodes) {
      return el("section", { class: "card dag-card", id: "dagCard" }, [
        el("div", { class: "card-header" }, [el("div", { class: "card-title" }, [icon("route"), doc.createTextNode("Dependency frontier")])]),
        el("div", { class: "card-body" }, [el("p", { class: "cell-sub", text: "No open tasks — nothing left to schedule." })])
      ]);
    }
    var wide = dg.fleet_wide_enough;
    var facts = el("div", { class: "dag-facts" }, [
      el("div", { class: "dag-fact" }, [el("span", { class: "dag-n", text: String(dg.critical_path_length) }), el("span", { class: "dag-l", text: "critical path (min steps)" })]),
      el("div", { class: "dag-fact" }, [el("span", { class: "dag-n", text: String(dg.max_frontier_width) }), el("span", { class: "dag-l", text: "widest frontier" })]),
      el("div", { class: "dag-fact" + (wide ? "" : " is-alert") }, [el("span", { class: "dag-n", text: String(dg.workers || 0) }), el("span", { class: "dag-l", text: wide ? "workers — fleet wide enough" : "workers — below the frontier" })]),
      el("div", { class: "dag-fact" }, [el("span", { class: "dag-n", text: String(dg.eta_tasks) }), el("span", { class: "dag-l", text: "steps to drain at this width" })])
    ]);
    var body = [facts];
    if (chart) body.push(el("div", { class: "dag-chart" }, [chart]));
    if (!dg.acyclic) {
      body.push(el("div", { class: "callout fail" }, [
        el("span", { class: "b-glyph", "aria-hidden": "true", text: GLYPH.fail }),
        el("div", { text: "The open dependency graph contains a CYCLE — these numbers are a floor, not a schedule. No worker can start inside a cycle." })
      ]));
    }
    var path = (dg.path || []).slice(0, 8);
    if (path.length) {
      var chain = el("div", { class: "dag-path" });
      path.forEach(function (n, i) {
        if (i) chain.appendChild(el("span", { class: "dag-arrow", "aria-hidden": "true", text: "→" }));
        var b = el("button", { class: "dag-step", type: "button", title: n.title || n.id,
          text: localId(n.id) });
        if (BY_ID[n.id]) b.addEventListener("click", function () { openEntity("task", BY_ID[n.id]); });
        else b.disabled = true;
        chain.appendChild(b);
      });
      body.push(el("div", { class: "dag-path-wrap" }, [
        el("span", { class: "dag-path-lbl", text: "longest chain" + ((dg.path || []).length > 8 ? " (first 8)" : "") }), chain]));
    }
    return el("section", { class: "card dag-card", id: "dagCard", "aria-labelledby": "dagTitle" }, [
      el("div", { class: "card-header" }, [
        el("div", { class: "card-title", id: "dagTitle" }, [icon("route"), doc.createTextNode("Dependency frontier")]),
        el("span", { class: "badge b-" + (wide ? "pass" : "warn"), text: wide ? "fleet wide enough" : "add workers" })
      ]),
      el("div", { class: "card-body" }, body)
    ]);
  }

  function telemetryLine(tel, cost) {
    /* Cost/latency from the OTLP GenAI metrics the workers emit — the standard's aggregate, never
       a bespoke field. Hidden entirely until a first instrumented run exists, because a zeroed
       cost line reads as a free fleet rather than an unmeasured one. */
    if (!tel || !tel.runs) return null;
    var toks = (tel.input_tokens || 0) + (tel.output_tokens || 0);
    var text = tel.runs + (tel.runs === 1 ? " run" : " runs")
      + (toks ? "  ·  " + fmtInt(tel.input_tokens || 0) + " in / " + fmtInt(tel.output_tokens || 0) + " out tokens" : "")
      + (tel.p50_run_ms != null ? "  ·  p50 " + (tel.p50_run_ms >= 1000 ? (tel.p50_run_ms / 1000).toFixed(1) + "s" : Math.round(tel.p50_run_ms) + "ms") : "");
    if (cost && cost.total_cost_usd) {
      text += "  ·  $" + cost.total_cost_usd.toFixed(2) + " spent";
      if (cost.avg_cost_per_done_task_usd != null) text += " ($" + cost.avg_cost_per_done_task_usd.toFixed(2) + "/task)";
      if (cost.projected_cost_to_drain_usd != null) text += "  ·  ≈$" + cost.projected_cost_to_drain_usd.toFixed(2) + " to drain";
    }
    text += "  ·  via OTLP";
    return el("div", { class: "progress-velocity progress-telemetry", text: text });
  }

  function commandStrip(L) {
    L = L || {};
    var fleet = L.fleet || [], attention = L.attention || [], readiness = L.readiness || {};
    var productive = fleet.filter(function (c) { return c.status === "working"; }).length;
    var stalled = fleet.filter(function (c) { return c.status === "stalled"; }).length;
    var primary = productive ? (productive + (productive === 1 ? " worker is moving work" : " workers are moving work"))
      : readiness.ready ? (readiness.ready + " ready for pickup") : "The ready queue is drained";
    return el("section", { class: "overview-signal-strip", "aria-label": "Command deck status" }, [
      el("div", { class: "signal-primary" }, [
        el("span", { class: "live-orb", "aria-hidden": "true" }),
        el("strong", { text: "Command deck" }),
        el("span", { text: primary })
      ]),
      el("div", { class: "signal-meta" }, [
        el("span", { text: (readiness.ready || 0) + " ready" }),
        el("span", { text: productive + " productive" }),
        stalled ? el("span", { class: "signal-warn", text: stalled + " stalled" }) : null,
        el("span", { text: attention.length + " need attention" })
      ].filter(Boolean))
    ]);
  }

  function deliveryCard(deliv) {
    if (!deliv || !deliv.counts) return null;
    var c = deliv.counts, measured = deliv.measured || {}, notes = deliv.notes || {};
    var legs = [
      { key: "verified", label: "Verified", value: c.verified, measured: measured.verified !== false },
      { key: "landing", label: "Landed", value: c.landed, measured: measured.landing !== false },
      { key: "release", label: "Released", value: c.deployed, measured: measured.release !== false },
      { key: "live", label: "Live", value: c.live, measured: measured.live !== false }
    ];
    var flow = el("div", { class: "delivery-flow" });
    legs.forEach(function (leg, i) {
      var complete = leg.measured && c.done > 0 && leg.value === c.done;
      flow.appendChild(el("div", { class: "delivery-leg " + (!leg.measured ? "is-unknown" : complete ? "is-complete" : "is-partial"),
        title: leg.measured ? (leg.value + " of " + c.done) : (notes[leg.key] || "unmeasured") }, [
        el("span", { class: "delivery-n", text: leg.measured ? String(leg.value == null ? 0 : leg.value) : "?" }),
        el("span", { class: "delivery-l", text: leg.label }),
        el("span", { class: "delivery-of", text: "of " + c.done })
      ]));
      if (i < legs.length - 1) flow.appendChild(el("span", { class: "delivery-arrow", "aria-hidden": "true", text: "→" }));
    });
    var notesList = legs.filter(function (leg) { return !leg.measured && notes[leg.key]; }).map(function (leg) {
      return el("li", { text: leg.label + ": " + notes[leg.key] });
    });
    return el("section", { class: "card delivery-card", id: "deliveryCard", "aria-labelledby": "deliveryTitle" }, [
      el("div", { class: "card-header" }, [
        el("div", { class: "card-title", id: "deliveryTitle" }, [icon("route"), doc.createTextNode("Delivery truth")]),
        el("span", { class: "badge b-" + (c.live === c.done && c.done ? "pass" : "warn"), text: c.done ? (c.live + "/" + c.done + " live") : "nothing done yet" })
      ]),
      el("div", { class: "card-body" }, [flow, notesList.length ? el("ul", { class: "delivery-notes" }, notesList) : null].filter(Boolean))
    ]);
  }

  function progressHero(P, rd, fleet, tel, cost, wipSt) {
    P = P || {};
    var pct = P.pct || 0;
    var perHr = P.last_1h || 0;
    var ready = (rd && rd.ready) || 0;
    var working = (fleet || []).filter(function (c) { return c.status === "working"; }).length;
    var stalled = (fleet || []).filter(function (c) { return c.status === "stalled"; }).length;
    var etaMin = (perHr > 0 && ready > 0) ? Math.round(ready / (perHr / 60)) : null;
    var vel = "≈ " + perHr + " tasks/hr"
      + (working ? "  ·  " + working + (working === 1 ? " worker working" : " workers working") : "")
      + (stalled ? "  ·  " + stalled + " stalled" : "")
      + (ready ? "  ·  " + ready + " ready" + (etaMin != null ? " (≈ " + fmtAge(etaMin * 60) + " to drain)" : "") : "  ·  ready queue drained");
    if (wipSt && wipSt.ceiling) {
      vel += "  ·  WIP " + wipSt.active + "/" + wipSt.ceiling + (wipSt.saturated ? " (saturated)" : "");
    }
    var telLine = telemetryLine(tel, cost);
    var stats = el("div", { class: "progress-stats" }, [
      el("div", { class: "pstat" }, [el("span", { class: "pstat-num", "data-countup": "ct", text: String(P.completed_total || 0) }), el("span", { class: "pstat-lbl", text: "completed, total" })]),
      el("div", { class: "pstat is-up" }, [el("span", { class: "pstat-num", "data-countup": "h1", text: "+" + (P.last_1h || 0) }), el("span", { class: "pstat-lbl", text: "last hour" })]),
      el("div", { class: "pstat" }, [el("span", { class: "pstat-num", "data-countup": "h24", text: "+" + (P.last_24h || 0) }), el("span", { class: "pstat-lbl", text: "last 24h" })]),
      el("div", { class: "pstat" + (rd && rd.needs_spec ? " is-alert" : "") }, [
        el("span", { class: "pstat-num", text: String((rd && rd.needs_spec) || 0) }),
        el("span", { class: "pstat-lbl", text: "need spec before a worker can pull" })]),
      /* LANDED IS A MEASUREMENT, NOT A SUBTRACTION. Where the ancestry probe cannot run there is
         no number to print: the tile renders "? / done" with the caveat, because done-minus-zero-
         unlanded would assert every done task is on the branch from a question nobody asked. */
      P.landing_measured === false
        ? el("div", { class: "pstat is-unmeasured", title: P.landing_note || "landing unverifiable in this context" }, [
            el("span", { class: "pstat-num", text: "? / " + (P.done || 0) }),
            el("span", { class: "pstat-lbl", text: "landed — unverifiable here" })])
        : el("div", { class: "pstat" + (P.unlanded ? " is-alert" : (P.landed_unmeasured ? " is-unmeasured" : "")) }, [
            el("span", { class: "pstat-num", text: (P.landed != null ? P.landed : "?") + " / " + (P.done || 0) }),
            el("span", { class: "pstat-lbl", text: P.unlanded ? "landed (" + P.unlanded + " NOT on the branch)"
              : P.landed_unmeasured ? "landed (" + P.landed_unmeasured + " never measured)" : "landed on the branch" })]),
      /* The one tile that speaks about PRODUCTION. Unmeasured liveness prints "?" rather than
         borrowing the landed number. */
      el("div", { class: "pstat" + (P.live != null && !P.live_attested ? " is-unmeasured" : "") }, [
        el("span", { class: "pstat-num", text: (P.live != null ? P.live : "?") + " / " + (P.done || 0) }),
        el("span", { class: "pstat-lbl", text: P.live == null ? "live — unprobed here"
          : P.live_attested ? "live in production" : "live — unattested probe" })])
    ]);
    return el("section", { class: "card progress-hero" }, [
      el("div", { class: "progress-top" }, [
        el("div", { class: "progress-pctwrap" }, [
          el("span", { class: "progress-pct", "data-countup": "pct", text: pct + "%" }),
          el("span", { class: "progress-sub", text: (P.done || 0) + " of " + (P.total || 0) + " tasks in scope" })
        ]),
        stats
      ]),
      el("div", { class: "progress-track" }, [el("div", { class: "progress-fill", style: "width:" + Math.min(100, pct) + "%" })]),
      el("div", { class: "progress-velocity", text: vel })
    ].concat(telLine ? [telLine] : []).concat([
      el("div", { class: "progress-spark-row" }, [
        el("span", { class: "progress-spark-lbl", text: "completions / 5 min" }),
        sparkline(P.spark, P.spark_bucket_s)
      ])
    ]));
  }

  function agentCard(c) {
    var head = el("div", { class: "agent-head" }, [
      el("span", { class: "agent-dot s-" + c.status, "aria-hidden": "true" }),
      el("span", { class: "agent-name", text: c.agent }),
      el("span", { class: "agent-status s-" + c.status, text: c.status })
    ]);
    var now = c.task
      ? el("div", { class: "agent-now" }, [
          el("span", { class: "agent-now-lbl", text: "on" }),
          el("span", { class: "agent-now-task", text: c.task }),
          el("span", { class: "agent-now-age", text: c.age_s != null ? fmtAge(c.age_s) : "" })
        ])
      : el("div", { class: "agent-now is-idle", text: c.idle_s != null ? ("last active " + fmtAge(c.idle_s) + " ago") : "idle" });
    var kids = [head, now];
    if (c.plan_total > 0) {
      var ppct = c.plan_pct != null ? c.plan_pct : 0;
      kids.push(el("div", { class: "agent-prog" }, [
        el("div", { class: "agent-prog-track" }, [el("div", { class: "agent-prog-fill", style: "width:" + ppct + "%" })]),
        el("div", { class: "agent-prog-lbl" }, [
          el("span", { class: "agent-prog-steps", text: "step " + (c.plan_done || 0) + "/" + c.plan_total }),
          el("span", { class: "agent-prog-cur", text: c.step || "" })
        ])
      ]));
    }
    if (c.done_total) kids.push(el("div", { class: "agent-done", text: c.done_total + " completed on this board" }));
    kids.push(el("ul", { class: "agent-trail" }, (c.trail || []).slice(0, 4).map(function (t) {
      return el("li", { class: "trail-item t-" + (t.action || "") }, [
        el("span", { class: "trail-action", text: t.action }),
        el("span", { class: "trail-title", text: t.title }),
        el("time", { class: "trail-time rel-time", datetime: t.ts || "", "data-ts": t.ts || "", text: relativeTime(t.ts) })
      ]);
    })));
    var interactive = c.task_id && BY_ID[c.task_id];
    var card = el(interactive ? "button" : "article", { class: "agent-card s-" + c.status,
      type: interactive ? "button" : null,
      "data-seq": String((c.trail && c.trail[0] && c.trail[0].seq) || ""),
      "data-agent": c.agent,
      "aria-label": c.agent + " " + c.status + (c.task ? " on " + c.task : "") }, kids);
    if (interactive) card.addEventListener("click", function () { openEntity("task", BY_ID[c.task_id]); });
    return card;
  }

  function workerHealthRow(h) {
    if (!h) return null;
    var parts = [];
    if (h.receipts) {
      parts.push("receipts " + (h.receipts - h.failed) + "/" + h.receipts + " green"
        + (h.fail_rate_pct == null ? "" : " (" + h.fail_rate_pct + "% failed)"));
    } else {
      parts.push("no receipts recorded in the last " + h.window + " — outcome unmeasured");
    }
    parts.push(h.seats_with_done_work + (h.seats_with_done_work === 1 ? " seat" : " seats") + " with done work");
    if (h.stalled_now) parts.push(h.stalled_now + " stalled now");
    var top = (h.tasks_per_worker || []).slice(0, 4).map(function (r) {
      return String(r.worker || "").replace(/^worker-/, "") + " " + r.done;
    }).join("  ·  ");
    var kids = [el("span", { text: "Fleet health: " + parts.join("  ·  ") })];
    if (top) kids.push(el("span", { style: "display:block", text: "tasks per worker: " + top }));
    return el("p", { class: "cell-sub fleet-health", text: null }, kids);
  }

  function failureModes(fm) {
    if (!fm || !fm.total) return null;
    var body = el("div", { class: "card-body" });
    var cats = Object.keys(fm.categories || {}).map(function (c) { return c + " " + fm.categories[c]; }).join("  ·  ");
    body.appendChild(el("p", { class: "cell-sub", style: "margin-bottom:10px",
      text: fm.total + " recorded refusal" + (fm.total === 1 ? "" : "s")
            + (cats ? "  ·  " + cats : "")
            + (fm.unclassified ? "  ·  " + fm.unclassified + " unnamed" : "") }));
    var max = (fm.modes || []).reduce(function (m, r) { return Math.max(m, r.count); }, 1);
    (fm.modes || []).forEach(function (r) {
      body.appendChild(el("div", { class: "phase-row" }, [
        el("span", { class: "phase-name", title: r.category, text: r.mode }),
        el("div", { class: "phase-track" }, [el("div", { class: "phase-fill", style: "width:" + Math.round(100 * r.count / max) + "%" })]),
        el("span", { class: "phase-pct", text: String(r.count) })
      ]));
    });
    return el("section", { class: "card", id: "failureCard", "aria-labelledby": "failureModesTitle" }, [
      el("div", { class: "card-header" }, [
        el("div", { class: "card-title", id: "failureModesTitle" }, [icon("warning"), doc.createTextNode("Failure modes")])]),
      body
    ]);
  }

  function fleetView(fleet, health) {
    fleet = fleet || [];
    var working = fleet.filter(function (c) { return c.status === "working"; }).length;
    var stalled = fleet.filter(function (c) { return c.status === "stalled"; }).length;
    var body = el("div", { class: "fleet-grid" });
    if (fleet.length) fleet.forEach(function (c) { body.appendChild(agentCard(c)); });
    else body.appendChild(el("div", { class: "fleet-empty", text: "No workers active. Launch one to start the fleet." }));
    var launch = el("a", { class: "text-action", href: "#", "data-launch": "1", "data-count": "1", hidden: "hidden",
                           text: "+ Launch worker" });
    return el("section", { class: "card fleet-card", id: "fleetCard", "aria-labelledby": "fleetTitle" }, [
      el("div", { class: "card-header" }, [
        el("div", { class: "card-title", id: "fleetTitle" }, [icon("users"),
          doc.createTextNode("Fleet" + (working ? "  ·  " + working + " working now" : "") + (stalled ? "  ·  " + stalled + " stalled" : ""))]),
        launch
      ]),
      el("div", { class: "card-body fleet-body" }, [workerHealthRow(health), body].filter(Boolean))
    ]);
  }

  function readinessRail(rd) {
    rd = rd || {};
    var rows = [];
    function group(label, n, items, tone, why) {
      if (!n) return;
      var list = el("div", { class: "ready-items" });
      (items || []).forEach(function (it) {
        var b = el("button", { class: "ready-item", type: "button", text: it.title || localId(it.id),
                               "data-entity-id": it.id,
                               title: it.id + (it.not_before ? (" · waits until " + it.not_before) : "") });
        if (BY_ID[it.id]) b.addEventListener("click", function () { openEntity("task", BY_ID[it.id]); });
        else b.disabled = true;
        list.appendChild(b);
      });
      rows.push(el("div", { class: "ready-group t-" + tone }, [
        el("div", { class: "ready-head" }, [
          el("span", { class: "ready-n", text: String(n) }),
          el("span", { class: "ready-lbl", text: label }),
          el("span", { class: "ready-why", text: why })
        ]),
        list
      ]));
    }
    group("ready to pull", rd.ready, rd.ready_top, "pass", "unblocked and specced — a worker can take these now");
    group("need spec", rd.needs_spec, rd.needs_spec_top, "warn", "unblocked but no verification_command — a worker would stall");
    group("waiting on a timer", rd.snoozed, rd.snoozed_top, "info", "deferred, not drained — they return on their own");
    if (!rows.length) rows.push(el("p", { class: "cell-sub", text: "No todo work on the board." }));
    return el("section", { class: "card ready-card", id: "readyCard", "aria-labelledby": "readyTitle" }, [
      el("div", { class: "card-header" }, [
        el("div", { class: "card-title", id: "readyTitle" }, [icon("bolt"), doc.createTextNode("Work queue")])]),
      el("div", { class: "card-body" }, rows)
    ]);
  }

  /* ---- overview composition ---- */
  function donut(pct, ok) {
    var r = 52, c = 2 * Math.PI * r, off = c * (1 - pct / 100);
    var s = svgEl("svg", { viewBox: "0 0 128 128", width: "128", height: "128", role: "img",
      "aria-label": pct + "% of tasks done" });
    function circle(cls, dash) {
      var attrs = { cx: "64", cy: "64", r: String(r), fill: "none", "stroke-width": "12", class: cls };
      if (dash != null) {
        attrs["stroke-dasharray"] = c.toFixed(1); attrs["stroke-dashoffset"] = dash.toFixed(1);
        attrs["stroke-linecap"] = "round"; attrs.transform = "rotate(-90 64 64)";
      }
      return svgEl("circle", attrs);
    }
    s.appendChild(circle("d-track"));
    s.appendChild(circle("d-val" + (ok ? "" : " fail"), off));
    var t = svgEl("text", { x: "64", y: "62", "text-anchor": "middle", class: "d-center", "font-size": "26" });
    t.textContent = pct + "%";
    var t2 = svgEl("text", { x: "64", y: "80", "text-anchor": "middle", class: "d-sub", "font-size": "11" });
    t2.textContent = "done";
    s.appendChild(t); s.appendChild(t2);
    return el("div", { class: "donut" }, [s]);
  }

  function buildOverview() {
    var pane = el("div", { class: "tab-content", id: "tab-overview", role: "tabpanel",
      "aria-labelledby": "tab-btn-overview", tabindex: "0" });
    var scroll = el("div", { class: "overview-scroll" });
    var au = D.audit || {}, b = D.build || {}, L = live();
    var activity = L.activity || [];

    scroll.appendChild(commandStrip(L));
    scroll.appendChild(progressHero(L.progress, L.readiness, L.fleet, L.telemetry, L.cost, L.wip));
    scroll.appendChild(el("div", { class: "operations-grid" }, [
      fleetView(L.fleet, L.worker_health), attentionRail(L.attention)
    ]));

    var mid = el("div", { class: "ov-grid" }, [adherenceCard(L.adherence), readinessRail(L.readiness)]);
    scroll.appendChild(mid);

    var delivery = deliveryCard(L.delivery);
    if (delivery) scroll.appendChild(delivery);

    var dc = dagCard(L.dag);
    if (dc) scroll.appendChild(dc);

    // activity feed
    var feed = el("div", { class: "activity-feed" });
    if (activity.length) {
      var newest = activity[0] && activity[0].seq;
      activity.slice(0, 24).forEach(function (ev) { feed.appendChild(activityItem(ev, ev.seq === newest)); });
    } else {
      feed.appendChild(el("p", { class: "cell-sub", text: "No canonical activity recorded yet." }));
    }
    var actCard = el("section", { class: "card activity-card", id: "activityCard" }, [
      el("div", { class: "card-header" }, [
        el("div", { class: "card-title" }, [icon("pulse"), doc.createTextNode("Live activity")]),
        el("span", { class: "cell-sub", text: "cursor " + ((L.cursor || {}).seq || 0) })
      ]),
      feed
    ]);

    // audit card
    var auBody = el("div", { class: "card-body" });
    auBody.appendChild(el("p", { class: "cell-sub", style: "margin-bottom:12px",
      text: "Computed per request (never a cached boolean) · exit " + (au.exit_code) + " · critical " + ((au.counts || {}).critical || 0) + " · high " + ((au.counts || {}).high || 0) + " · warn " + ((au.counts || {}).warn || 0) }));
    if ((au.violations || []).length) {
      au.violations.slice(0, 12).forEach(function (v) {
        auBody.appendChild(el("div", { class: "callout " + (v.severity === "warn" ? "warn" : "fail") }, [
          el("span", { class: "b-glyph", "aria-hidden": "true", text: GLYPH[v.severity === "warn" ? "warn" : "fail"] }),
          el("div", null, [el("strong", { text: v.id + " " }), doc.createTextNode(v.observed || ""),
            v.remediation ? el("div", { class: "cell-sub", style: "margin-top:4px", text: "→ " + v.remediation }) : null])
        ]));
      });
    } else {
      auBody.appendChild(el("div", { class: "callout info" }, [
        el("span", { class: "b-glyph", "aria-hidden": "true", text: GLYPH.pass }),
        el("div", { text: "No violations — independently verified." })]));
    }
    var auCard = el("section", { class: "card", id: "auditCard" }, [
      el("div", { class: "card-header" }, [
        el("div", { class: "card-title" }, [icon("warning"), doc.createTextNode("Audit")]),
        el("span", { class: "badge b-" + (au.ok ? "pass" : "fail"), text: au.ok ? "PASS" : "FAIL" })]),
      auBody
    ]);

    // phases
    var phBody = el("div", { class: "card-body" });
    (D.phases || []).forEach(function (p) {
      phBody.appendChild(el("div", { class: "phase-row" }, [
        el("span", { class: "phase-name", text: p.name }),
        el("div", { class: "phase-track" }, [el("div", { class: "phase-fill" + (p.pct >= 100 ? " full" : ""), style: "width:" + (p.pct || 0) + "%" })]),
        el("span", { class: "phase-pct", text: p.done + "/" + p.total })
      ]));
    });
    if (!(D.phases || []).length) phBody.appendChild(el("p", { class: "cell-sub", text: "No phases." }));
    var phCard = el("section", { class: "card" }, [
      el("div", { class: "card-header" }, [el("div", { class: "card-title" }, [icon("checks"), doc.createTextNode("Phase progress")])]),
      el("div", { class: "card-body ov-donutrow" }, [donut((L.progress || {}).pct || 0, au.ok), phBody])
    ]);

    scroll.appendChild(el("div", { class: "ov-grid" }, [actCard, phCard]));

    var fm = failureModes(L.failure_modes);
    scroll.appendChild(el("div", { class: "ov-grid" }, [auCard, fm].filter(Boolean)));

    function ci(label, val) { return el("span", { class: "ci" }, [doc.createTextNode(label + " "), el("code", { text: val == null ? "—" : String(val) })]); }
    scroll.appendChild(el("section", { class: "card" }, [
      el("div", { class: "card-header" }, [
        el("div", { class: "card-title" }, [icon("rocket"), doc.createTextNode("Build coherence")]),
        el("span", { class: "badge b-" + (b.coherent === true ? "pass" : (b.coherent === false ? "fail" : "stale")),
                     text: b.coherent === true ? "coherent" : (b.coherent === false ? "drift" : "unverified") })]),
      el("div", { class: "card-body" }, [el("div", { class: "coherence-strip" }, [
        ci("repo", b.repo), ci("deploy", b.deploy), ci("stamped sha", b.sha), ci("HEAD", b.head), ci("served", b.served_sha)
      ])])
    ]));

    pane.appendChild(scroll);
    return pane;
  }

  /* ============================ MODAL ============================ */
  function row(label, valNode) { return el("div", { class: "detail-row" }, [el("div", { class: "detail-label", text: label }), valNode && valNode.nodeType ? el("div", { class: "detail-value" }, [valNode]) : el("div", { class: "detail-value", text: String(valNode) })]); }
  function rowMono(label, val) { return el("div", { class: "detail-row" }, [el("div", { class: "detail-label", text: label }), el("div", { class: "detail-value mono", text: val == null ? "—" : String(val) })]); }
  function section(title, ic, rows) { return el("div", { class: "detail-section" }, [el("div", { class: "detail-section-title" }, [icon(ic), doc.createTextNode(title)])].concat(rows.filter(Boolean))); }
  function chip(type, id) {
    var rec = BY_ID[id];
    var t = (String(id).split(":")[1]) || type;
    var c = el("span", { class: "badge chip-link", title: id, text: localId(id) });
    if (rec) c.addEventListener("click", function (e) { e.stopPropagation(); openEntity(t, rec); });
    return c;
  }
  function chipRow(ids, type) {
    if (!ids || !ids.length) return null;
    return el("div", { class: "chip-row" }, ids.map(function (id) { return chip(type, id); }));
  }

  function openEntity(type, r) {
    var role = type === "deploy" ? (r.audit_ok ? "pass" : "fail") : roleOf(type, r.status || r.maturity);
    var iconName = { task: "checks", adr: "branch", feat: "package", gap: "warning", cap: "stack", deploy: "rocket" }[type] || "info";
    var title = r.title || r.name || (r.number != null ? ("ADR " + r.number) : localId(r.id));
    var body = el("div");

    var idRows = [
      rowMono("ID", r.id),
      r.legacy_ref ? rowMono("Legacy", r.legacy_ref) : null,
      r.status ? row("Status", type === "task" ? taskStatusBadge(r) : badge(type, r.status)) : null,
      r.severity ? row("Severity", el("span", { class: "sev-badge sev-" + r.severity, text: r.severity })) : null,
      r.maturity ? row("Maturity", badge("cap", r.maturity)) : null,
      r.phase ? row("Phase", r.phase) : null,
      r.priority ? row("Priority", r.priority) : null,
      r.number != null ? rowMono("Number", r.number) : null,
      r.version != null ? rowMono("Version", r.version) : null
    ];
    var detailRows = [
      r.summary ? row("Summary", r.summary) : null,
      r.acceptance ? row("Acceptance", r.acceptance) : null,
      r.source ? row("Source", r.source) : null,
      r.evidence_uri ? row("Evidence", r.evidence_uri) : null,
      r.needs ? row("Needs", r.needs) : null,
      r.build ? rowMono("Build", r.build) : null,
      r.sha ? rowMono("SHA", r.sha) : null,
      r.at ? rowMono("At", r.at) : null
    ];
    var grid = el("div", { class: "detail-grid" + (detailRows.filter(Boolean).length ? "" : " one") }, [section("Identity", "info", idRows)]);
    if (detailRows.filter(Boolean).length) grid.appendChild(section("Detail", iconName, detailRows));
    body.appendChild(grid);

    // LIVE: what is happening to this task right now, and what proved it done.
    if (type === "task") {
      var lease = leaseOf(r.id), prog = taskProgress(r), rec = receiptOf(r);
      var liveRows = [];
      if (lease) {
        liveRows.push(row("Held by", el("span", { class: "lease-chip" + (lease.stalled ? " is-stalled" : "") }, [
          el("span", { class: "lease-dot", "aria-hidden": "true" }), doc.createTextNode(lease.agent + " · " + fmtAge(lease.age_s))])));
      }
      if (prog) {
        liveRows.push(row("Plan", el("div", null, [
          el("div", { class: "tcard-track" }, [el("div", { class: "tcard-fill", style: "width:" + prog.pct + "%" })]),
          el("div", { class: "cell-sub", text: "step " + prog.done + "/" + prog.total + (prog.step ? (" — " + prog.step) : "") })
        ])));
      }
      if (r.verification_command) liveRows.push(rowMono("Verification command", r.verification_command));
      if (rec) {
        liveRows.push(row("Receipt", el("div", { class: "receipt-box" + (rec.exit_code === 0 ? " ok" : " bad") }, [
          el("div", { class: "mono", text: rec.command || "" }),
          el("div", { class: "cell-sub", text: "exit " + rec.exit_code + (rec.ran_by ? (" · ran by " + rec.ran_by) : "") + (rec.ran_at ? (" · " + rec.ran_at) : "") })
        ])));
      } else if (r.status === "done") {
        liveRows.push(row("Receipt", el("div", { class: "callout warn" }, [
          el("span", { class: "b-glyph", "aria-hidden": "true", text: GLYPH.warn }),
          el("div", { text: "This task is done with no recorded verification_run. Under the receipt gate, done is granted by an exit-0 receipt — this one was recorded another way." })])));
      }
      if (liveRows.length) body.appendChild(el("div", { class: "detail-grid one" }, [section("Live", "pulse", liveRows)]));
    }

    var links = [];
    if (r.tasks && r.tasks.length) links.push(row("Tasks", chipRow(r.tasks, "task")));
    if (r.deps && r.deps.length) links.push(row("Deps", chipRow(r.deps, "task")));
    if (r.deps_unmet && r.deps_unmet.length) links.push(row("Unmet deps", chipRow(r.deps_unmet, "task")));
    if (r.addressed_by && r.addressed_by.length) links.push(row("Addressed by", chipRow(r.addressed_by, "task")));
    if (r.implements && r.implements.length) links.push(row("Implements", chipRow(r.implements, "feat")));
    if (r.adrs && r.adrs.length) links.push(row("ADRs", chipRow(r.adrs, "adr")));
    if (r.decided_by && r.decided_by.length) links.push(row("Decided by", chipRow(r.decided_by, "adr")));
    if (r.superseded_by && r.superseded_by.length) links.push(row("Superseded by", chipRow(r.superseded_by, "adr")));
    if (r.verified_by && r.verified_by.length) links.push(row("Verified by", el("div", null, r.verified_by.map(function (s) { return el("div", { class: "detail-prose", text: "• " + s }); }))));
    if (links.length) body.appendChild(el("div", { class: "detail-grid one" }, [section("Links & evidence", "branch", links)]));

    ["context_md", "decision_md", "consequences_md"].forEach(function (f) {
      if (r[f]) body.appendChild(el("div", { class: "detail-grid one" }, [section(f.replace("_md", "").replace(/^./, function (c) { return c.toUpperCase(); }), "info", [el("div", { class: "detail-prose", text: r[f] })])]));
    });

    if (r.provenance) {
      var pv = r.provenance;
      body.appendChild(el("div", { class: "detail-grid one" }, [section("Provenance", "info", [
        rowMono("Created", pv.created_at), rowMono("Updated", pv.updated_at), pv.agent ? rowMono("Agent", pv.agent) : null,
        pv.commits && pv.commits.length ? rowMono("Commits", pv.commits.join(", ")) : null
      ])]));
    }
    openModal(role, title, r.id, iconName, body);
    try { history.replaceState(null, "", "#" + type + "-" + localId(r.id)); } catch (e) {}
  }

  // The element that opened the dialog, so closing can hand focus back where it came from.
  // Losing it drops the keyboard user at the top of the document with their place gone.
  var _modalOpener = null;
  var FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),' +
                  'textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';
  function _modalFocusables() {
    var m = doc.getElementById("universalModal");
    if (!m) return [];
    return Array.prototype.filter.call(m.querySelectorAll(FOCUSABLE), function (n) {
      return n.offsetParent !== null || n === doc.activeElement;
    });
  }
  function _trapTab(e) {
    if (e.key !== "Tab") return;
    var f = _modalFocusables();
    if (!f.length) return;
    var first = f[0], last = f[f.length - 1];
    // Tab off the end wraps to the start (and Shift+Tab the other way) — without this the focus
    // ring walks out into the inert background and the dialog only LOOKS modal.
    if (e.shiftKey && doc.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && doc.activeElement === last) { e.preventDefault(); first.focus(); }
  }
  function openModal(role, title, subtitle, iconName, bodyNode) {
    var m = doc.getElementById("universalModal");
    var box = doc.getElementById("modalIcon"); box.className = "modal-icon t-" + role; box.textContent = ""; box.appendChild(icon(iconName));
    doc.getElementById("modalTitle").textContent = title;
    doc.getElementById("modalSubtitle").textContent = subtitle || "";
    var b = doc.getElementById("modalBody"); b.textContent = ""; b.appendChild(bodyNode); b.scrollTop = 0;
    _modalOpener = (doc.activeElement && doc.activeElement !== doc.body) ? doc.activeElement : null;
    m.classList.add("show");
    // inert takes the whole background out of the tab order AND the accessibility tree, which is
    // what aria-modal only PROMISES; aria-hidden is the fallback where inert is unsupported.
    var shell = doc.getElementById("appShell");
    if (shell) { shell.inert = true; shell.setAttribute("aria-hidden", "true"); }
    doc.addEventListener("keydown", _trapTab, true);
    var c = doc.getElementById("modalClose"); if (c) c.focus();
  }
  function closeModal() {
    var m = doc.getElementById("universalModal");
    if (m) m.classList.remove("show");
    var shell = doc.getElementById("appShell");
    if (shell) { shell.inert = false; shell.removeAttribute("aria-hidden"); }
    doc.removeEventListener("keydown", _trapTab, true);
    if (_modalOpener && doc.contains(_modalOpener)) { try { _modalOpener.focus(); } catch (e) {} } // absorbs: element detached
    _modalOpener = null;
  }

  /* ============================ TOAST + STATUS ============================ */
  var TOAST_ICON = { success: "check", error: "xc", info: "info" };
  function toast(message, type) {
    type = type || "info";
    var wrap = doc.getElementById("toastContainer"); if (!wrap) return;
    var t = el("div", { class: "toast " + type }, [icon(TOAST_ICON[type] || "info"), el("span", { text: message })]);
    wrap.appendChild(t);
    setTimeout(function () { t.classList.add("hiding"); setTimeout(function () { t.remove(); }, 300); }, 3800);
  }
  function celebrateTask(task) {
    var existing = doc.querySelector(".completion-celebration");
    if (existing) existing.remove();
    var rec = receiptOf(task);
    var proven = !!(rec && rec.exit_code === 0);
    var particles = el("span", { class: "completion-particles", "aria-hidden": "true" });
    for (var i = 0; i < 12; i++) particles.appendChild(el("i", { style: "--particle:" + i }));
    // The loudest moment on the board must not overstate what happened. `done` was either GRANTED
    // by an exit-0 receipt or merely recorded, and the celebration says which.
    var celebration = el("div", { class: "completion-celebration" + (proven ? "" : " not-proven"), role: "status", "aria-live": "assertive" }, [
      particles,
      el("span", { class: "completion-check", "aria-hidden": "true", text: proven ? "✓" : "◌" }),
      el("span", { class: "completion-copy" }, [
        el("strong", { text: proven ? "Task complete · verified" : "Task complete · no receipt" }),
        el("span", { text: task.title || localId(task.id) })
      ])
    ]);
    doc.body.appendChild(celebration);
    (global.requestAnimationFrame || setTimeout)(function () { celebration.classList.add("show"); });
    setTimeout(function () {
      celebration.classList.add("leave");
      setTimeout(function () { celebration.remove(); }, 520);
    }, 2600);
  }

  var LIVE = {
    source: null,
    cursor: ((live().cursor) || {}).seq || 0,
    lastEventAt: ((live().cursor) || {}).ts || null,
    state: "connecting",
    connected: false,
    failures: 0,
    dataHealthy: true,
    lastAppliedAt: Date.now(),
    etag: null,
    snapshotJSON: JSON.stringify(D),
    syncing: false,
    queued: false,
    fallbackTimer: null,
    reconnectTimer: null,
    integrityTimer: null
  };
  function setStatus(state, text, meta) {
    var p = doc.getElementById("statusPill"); if (!p) return;
    p.className = "status-pill" + (state ? " " + state : "");
    p.setAttribute("data-state", state || "live");
    LIVE.state = state || "live";
    var s = doc.getElementById("statusText"); if (s) s.textContent = text;
    var m = doc.getElementById("statusMeta"); if (m && meta != null) m.textContent = meta;
  }
  function announce(message) {
    var node = doc.getElementById("liveAnnouncer");
    if (node) node.textContent = message;
  }
  function tickClock() { var c = doc.getElementById("clock"); if (c) c.textContent = new Date().toTimeString().slice(0, 8); }
  function tickLiveMeta() {
    var meta = doc.getElementById("statusMeta");
    if (!meta) return;
    meta.textContent = "seq " + LIVE.cursor + " · " + (LIVE.lastEventAt ? relativeTime(LIVE.lastEventAt) : "awaiting event")
      + (!LIVE.dataHealthy ? " · data stale" : "");
  }
  function renderConnectionStatus(fallback) {
    if (doc.hidden) return setStatus("paused", "Paused");
    if (!LIVE.dataHealthy) return setStatus("degraded", LIVE.connected ? "Board stale" : "Offline");
    if (LIVE.connected) return setStatus("live", "Current");
    if (fallback === "polling") return setStatus("degraded", "Polling");
    return setStatus("reconnecting", LIVE.failures ? "Reconnecting" : "Connecting");
  }
  function paintBackdrop() {
    // The ambient field is a READOUT, not decoration: its brightness and tempo come from how many
    // workers are actually holding a lease, and its hue from whether anything needs a human. A
    // board nobody is working on is nearly still — which is the honest thing for it to look like.
    var L = live();
    var working = (L.fleet || []).filter(function (c) {
      return c.status === "working";
    }).length;
    var band = working >= 3 ? "many" : String(Math.min(2, working));
    var attn = L.attention || [];
    var worst = attn.reduce(function (m, a) {
      var tone = ATTN_TONE[a.kind] || "info";
      return tone === "fail" ? "fail" : (tone === "warn" && m !== "fail") ? "warn" : m;
    }, "calm");
    if (D.audit && D.audit.ok === false) worst = "fail";
    doc.body.setAttribute("data-fleet", band);
    doc.body.setAttribute("data-mood", worst);
  }

  function tickRelativeTimes() {
    // Ages keep moving between snapshots. Without this the feed freezes at "2m ago" on a quiet
    // board and the page looks disconnected precisely when it is healthy and simply idle.
    [].forEach.call(doc.querySelectorAll(".rel-time[data-ts]"), function (n) {
      n.textContent = relativeTime(n.getAttribute("data-ts"));
    });
  }

  function byId(rows) {
    var index = {};
    (rows || []).forEach(function (r) { if (r && r.id) index[r.id] = r; });
    return index;
  }
  function taskDelta(previous, next) {
    var before = byId(previous), after = byId(next), changed = {};
    Object.keys(after).forEach(function (id) {
      var old = before[id], now = after[id];
      if (!old) changed[id] = "created";
      else if (old.status !== now.status) changed[id] = now.status || "updated";
      else if (old.version !== now.version || JSON.stringify(old.plan || []) !== JSON.stringify(now.plan || [])) changed[id] = "progress";
    });
    return changed;
  }
  function refreshOverview() {
    var old = _panes.overview;
    if (!old || !old.parentNode) return;
    var scroll = old.querySelector(".overview-scroll");
    var top = scroll ? scroll.scrollTop : 0;
    var active = old.classList.contains("active");
    function keyOf(node) {
      if (!node || !old.contains(node)) return null;
      if (node.id) return "id:" + node.id;
      var attrs = ["data-focus-key", "data-entity-id", "data-task-id", "data-agent", "data-seq"];
      for (var i = 0; i < attrs.length; i++) {
        if (node.getAttribute && node.getAttribute(attrs[i])) return attrs[i] + ":" + node.getAttribute(attrs[i]);
      }
      return null;
    }
    function findKey(root, key) {
      if (!key) return null;
      if (key.slice(0, 3) === "id:") return doc.getElementById(key.slice(3));
      var cut = key.indexOf(":"), attr = key.slice(0, cut), value = key.slice(cut + 1);
      var nodes = root.querySelectorAll("[" + attr + "]");
      for (var i = 0; i < nodes.length; i++) if (nodes[i].getAttribute(attr) === value) return nodes[i];
      return null;
    }
    var focusKey = keyOf(doc.activeElement);
    var openerKey = keyOf(_modalOpener);
    var fresh = buildOverview();
    if (active) fresh.classList.add("active");
    fresh.classList.add("live-refresh");
    old.parentNode.replaceChild(fresh, old);
    _panes.overview = fresh;
    var freshScroll = fresh.querySelector(".overview-scroll");
    if (freshScroll) freshScroll.scrollTop = top;
    var restored = findKey(fresh, focusKey);
    if (restored) { try { restored.focus({ preventScroll: true }); } catch (e) { restored.focus(); } }
    if (openerKey) _modalOpener = findKey(fresh, openerKey) || _modalOpener;
    primeLaunchControls();
    paintBackdrop();
  }

  var PREFERS_REDUCED = !!(global.matchMedia && global.matchMedia("(prefers-reduced-motion: reduce)").matches);
  function flashClass(node, cls, ms) {
    if (!node) return;
    node.classList.remove(cls); void node.offsetWidth;   // restart the animation if already flashing
    node.classList.add(cls);
    setTimeout(function () { node.classList.remove(cls); }, ms || 1200);
  }
  function fleetSeqMap(fleet) {
    var m = {};
    (fleet || []).forEach(function (c) { m[c.agent] = Number((c.trail && c.trail[0] && c.trail[0].seq) || 0); });
    return m;
  }
  function countUp(node, to, opts) {
    if (!node) return;
    opts = opts || {};
    var raw = node.getAttribute("data-from") != null ? node.getAttribute("data-from") : node.textContent;
    var from = parseFloat(String(raw).replace(/[^0-9.\-]/g, "")) || 0;
    to = Number(to) || 0;
    var pfx = opts.prefix || "", sfx = opts.suffix || "", dec = opts.decimals || 0;
    var fmt = function (v) { return pfx + (dec ? v.toFixed(dec) : String(Math.round(v))) + sfx; };
    if (PREFERS_REDUCED || from === to) { node.textContent = fmt(to); return; }
    var start = null, dur = 700;
    function step(ts) {
      if (start == null) start = ts;
      var p = Math.min(1, (ts - start) / dur), e = 1 - Math.pow(1 - p, 3);   // easeOutCubic
      node.textContent = fmt(from + (to - from) * e);
      if (p < 1) global.requestAnimationFrame(step); else node.textContent = fmt(to);
    }
    global.requestAnimationFrame(step);
  }
  function reactCockpit(prevProg, prevFleet) {
    // The cockpit is rebuilt on every snapshot; make it REACT — numbers count up from their prior
    // value, a completion bumps the bar and the newest sparkline column, and an agent that just
    // acted flashes. Without this a live board redraws identically to a page reload and the
    // operator cannot tell what actually moved.
    var prog = live().progress || {};
    var jobs = {
      pct: [prevProg.pct || 0, prog.pct || 0, { suffix: "%" }],
      ct: [prevProg.completed_total || 0, prog.completed_total || 0, {}],
      h1: [prevProg.last_1h || 0, prog.last_1h || 0, { prefix: "+" }],
      h24: [prevProg.last_24h || 0, prog.last_24h || 0, { prefix: "+" }]
    };
    Object.keys(jobs).forEach(function (k) {
      var node = doc.querySelector('[data-countup="' + k + '"]');
      if (!node) return;
      node.setAttribute("data-from", String(jobs[k][0]));
      countUp(node, jobs[k][1], jobs[k][2]);
    });
    if ((prog.completed_total || 0) > (prevProg.completed_total || 0)) {
      flashClass(doc.querySelector(".progress-hero"), "bumped", 950);
      var bars = doc.querySelectorAll(".progress-hero .spark-bar");
      flashClass(bars[bars.length - 1], "pulsed", 950);
    }
    [].forEach.call(doc.querySelectorAll(".agent-card[data-agent]"), function (card) {
      var ag = card.getAttribute("data-agent"), seq = Number(card.getAttribute("data-seq") || 0);
      if (prevFleet[ag] != null && seq > prevFleet[ag]) flashClass(card, "just-acted", 1600);
    });
  }
  function reactToChanges(changes) {
    Object.keys(changes).forEach(function (id) {
      var safeId = id.replace(/"/g, '\\"');
      var sel = '[data-entity-id="' + safeId + '"], [data-task-id="' + safeId + '"]';
      [].forEach.call(doc.querySelectorAll(sel), function (node) {
        node.classList.add("is-live-change", "change-" + changes[id]);
      });
      setTimeout(function () {
        [].forEach.call(doc.querySelectorAll(sel), function (node) {
          node.classList.remove("is-live-change", "change-" + changes[id]);
        });
      }, 1700);
      var current = BY_ID[id];
      if (current && ["done", "blocked", "in_progress"].indexOf(changes[id]) >= 0) {
        if (changes[id] === "done") celebrateTask(current);
        var rec = changes[id] === "done" ? receiptOf(current) : null;
        var proven = !!(rec && rec.exit_code === 0);
        toast((changes[id] === "done" ? (proven ? "Completed (verified): " : "Completed (no receipt): ")
              : changes[id] === "blocked" ? "Blocked: " : "Started: ") + (current.title || localId(id)),
          changes[id] === "done" ? (proven ? "success" : "info") : (changes[id] === "blocked" ? "error" : "info"));
      }
    });
  }

  function rerenderTabs(changes) {
    TABS.forEach(function (tab) {
      if (tab.key === "overview") return;
      if (tab.pick) tab.rows = tab.pick(D);
      if (tab._badge) tab._badge.textContent = String(tab.rows.length);
      if (tab._facetBar) renderFacetBar(tab);
      if (tab._tbody) { updateSortHeaders(tab); renderRows(tab); }
      if (tab._stage) renderTaskStage(tab, changes);
    });
  }

  function derivePhases() {
    var map = {};
    (D.tasks || []).forEach(function (task) {
      var name = task.phase || "Unphased";
      var row = map[name] || (map[name] = { name: name, done: 0, total: 0 });
      row.total += 1;
      if (task.status === "done") row.done += 1;
    });
    D.phases = Object.keys(map).sort(function (a, b) {
      return a.localeCompare(b, undefined, { numeric: true });
    }).map(function (name) {
      var row = map[name];
      row.pct = row.total ? Math.round(100 * row.done / row.total) : 0;
      return row;
    });
  }
  function publishClientState() {
    var json = JSON.stringify(D);
    var island = doc.getElementById("hub-data");
    if (island) island.textContent = json;
    LIVE.snapshotJSON = json;
    if (global.HubPalette && global.HubPalette.refresh) global.HubPalette.refresh(D);
  }

  function applySnapshot(next, reason) {
    if (!next || !next.tasks) throw new Error("incomplete Hub snapshot");
    var changes = taskDelta(D.tasks || [], next.tasks || []);
    var oldActivity = ((live().activity || [])[0] || {}).seq || 0;
    var prevProg = live().progress || {};
    var prevFleet = fleetSeqMap(live().fleet);
    D = next;
    rebuildIndex();
    rerenderTabs(changes);
    refreshOverview();
    reactCockpit(prevProg, prevFleet);
    publishClientState();
    reactToChanges(changes);

    var cursor = live().cursor || {};
    LIVE.cursor = cursor.seq || LIVE.cursor;
    LIVE.lastEventAt = cursor.ts || LIVE.lastEventAt;
    var n = Object.keys(changes).length;
    if (n) announce(n + (n === 1 ? " task changed." : " tasks changed."));
    else if (((live().activity || [])[0] || {}).seq > oldActivity) announce("New canonical Hub activity received.");
    LIVE.dataHealthy = true;
    LIVE.lastAppliedAt = Date.now();
    renderConnectionStatus(reason === "fallback" ? "polling" : null);
    tickLiveMeta();
  }

  var TYPE_COLLECTION = { task: "tasks", adr: "adrs", feat: "feats", gap: "gaps", cap: "caps",
                          deploy: "deploys", note: "notes" };
  function applyDelta(payload) {
    // DELTA CONSUME: patch only the changed entities into in-memory state and re-render. The wire
    // carries the changed rows, never the whole board. The cockpit blocks ride along in
    // payload.live so the hero, fleet and rail move with the entities instead of lagging a full
    // sync behind — the most-watched part of the page must not be the last to update.
    var changed = payload.changed || [];
    var prevTasks = (D.tasks || []).slice();
    var prevProg = live().progress || {};
    var prevFleet = fleetSeqMap(live().fleet);
    changed.forEach(function (ent) {
      var key = TYPE_COLLECTION[ent.type];
      if (!key) return;
      var rows = D[key] = D[key] || [];
      var i;
      for (i = 0; i < rows.length; i++) { if (rows[i].id === ent.id) break; }
      if (i < rows.length) rows[i] = ent; else rows.push(ent);
    });
    if (payload.live) {
      D.live = D.live || {};
      Object.keys(payload.live).forEach(function (k) {
        if (payload.live[k] != null) D.live[k] = payload.live[k];
      });
    }
    if (payload.audit) D.audit = Object.assign({}, D.audit || {}, payload.audit);
    derivePhases();
    var changes = taskDelta(prevTasks, D.tasks || []);
    rebuildIndex();
    rerenderTabs(changes);
    refreshOverview();
    reactCockpit(prevProg, prevFleet);
    reactToChanges(changes);
    publishClientState();
    var cursor = payload.cursor || {};
    LIVE.cursor = Math.max(LIVE.cursor, cursor.seq || 0);
    LIVE.dataHealthy = true;
    LIVE.lastAppliedAt = Date.now();
    renderConnectionStatus();
    tickLiveMeta();
  }

  function timedFetch(url, options) {
    if (!global.AbortController) return fetch(url, options);
    var controller = new global.AbortController();
    var timer = setTimeout(function () { controller.abort(); }, 9000);
    options = Object.assign({}, options || {}, { signal: controller.signal });
    return fetch(url, options).then(function (response) {
      clearTimeout(timer); return response;
    }, function (error) {
      clearTimeout(timer); throw error;
    });
  }

  function syncDelta(reason) {
    // FALLBACK to the full snapshot on: no base cursor, HTTP failure, parse error, or a cursor
    // GAP (the server head regressed below ours — a reset we cannot patch over).
    if (LIVE.syncing) { LIVE.queued = true; return Promise.resolve(); }
    var since = LIVE.cursor || 0;
    if (!since) return syncSnapshot(reason);
    LIVE.syncing = true;
    return timedFetch("delta.json?since=" + encodeURIComponent(since), {
      credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" }
    }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    }).then(function (payload) {
      var seq = payload && payload.cursor ? payload.cursor.seq : null;
      if (typeof seq !== "number" || seq < since) throw new Error("cursor gap");
      applyDelta(payload);
      LIVE.failures = 0;
    }).catch(function () {
      LIVE.syncing = false;                       // let the recovery full-sync take the slot
      return syncSnapshot((reason || "delta") + "-fallback");
    }).then(function (v) {
      LIVE.syncing = false;
      if (LIVE.queued) { LIVE.queued = false; syncDelta("queued"); }
      return v;
    });
  }
  function syncSnapshot(reason) {
    if (LIVE.syncing) { LIVE.queued = true; return Promise.resolve(); }
    LIVE.syncing = true;
    var button = doc.getElementById("refreshBtn");
    if (button) button.classList.add("is-syncing");
    var headers = { Accept: "application/json" };
    if (LIVE.etag) headers["If-None-Match"] = LIVE.etag;
    return timedFetch("?format=json", {
      credentials: "same-origin", cache: "no-store", headers: headers
    }).then(function (response) {
      if (response.status === 304) return { notModified: true };
      if (!response.ok) throw new Error("HTTP " + response.status);
      LIVE.etag = response.headers.get("ETag") || LIVE.etag;
      return response.json().then(function (snapshot) { return { snapshot: snapshot }; });
    }).then(function (result) {
      var snapshot = result && result.snapshot;
      if (snapshot && JSON.stringify(snapshot) !== LIVE.snapshotJSON) {
        applySnapshot(snapshot, reason || "sync");
      } else {
        LIVE.dataHealthy = true;
        LIVE.lastAppliedAt = Date.now();
        renderConnectionStatus(reason === "fallback" ? "polling" : null);
        tickLiveMeta();
      }
      LIVE.failures = 0;
    }).catch(function (error) {
      LIVE.failures += 1;
      LIVE.dataHealthy = false;
      renderConnectionStatus();
      announce("Live Hub synchronization is degraded. Retrying automatically.");
      if (reason === "manual") toast("Sync failed; automatic recovery is active.", "error");
    }).then(function () {
      LIVE.syncing = false;
      if (button) button.classList.remove("is-syncing");
      if (LIVE.queued) { LIVE.queued = false; syncSnapshot("queued"); }
    });
  }

  function stopFallback() {
    if (LIVE.fallbackTimer) clearTimeout(LIVE.fallbackTimer);
    LIVE.fallbackTimer = null;
  }
  function startFallback() {
    if (LIVE.fallbackTimer) return;
    var poll = function () {
      LIVE.fallbackTimer = null;
      syncSnapshot("fallback").then(function () {
        // Base cadence comes from the snapshot — the server owns the poll budget.
        var base = live().fallback_poll_ms || 2500;
        if (LIVE.state !== "live") LIVE.fallbackTimer = setTimeout(poll, Math.min(12000, base + LIVE.failures * 1250));
      });
    };
    LIVE.fallbackTimer = setTimeout(poll, 600);
  }
  function disconnectLive() {
    if (LIVE.source) LIVE.source.close();
    LIVE.source = null;
    LIVE.connected = false;
    if (LIVE.reconnectTimer) clearTimeout(LIVE.reconnectTimer);
    LIVE.reconnectTimer = null;
  }
  function connectLive() {
    disconnectLive();
    if (doc.hidden) return;
    if (!global.EventSource) { renderConnectionStatus("polling"); startFallback(); return; }
    setStatus("connecting", LIVE.failures ? "Reconnecting" : "Connecting");
    var source = new global.EventSource("live/events?since=" + encodeURIComponent(LIVE.cursor));
    LIVE.source = source;
    source.onopen = function () {
      LIVE.connected = true; LIVE.failures = 0;
      stopFallback(); renderConnectionStatus(); tickLiveMeta();
    };
    source.addEventListener("ready", function (event) {
      // Never bump the cursor past unconsumed events: if the stream starts ahead of our state,
      // pull the gap as a delta (which advances the cursor only after patching).
      try {
        var data = JSON.parse(event.data);
        if ((data.seq || 0) > LIVE.cursor) syncDelta("ready");
      } catch (error) {}
      tickLiveMeta();
    });
    source.addEventListener("heartbeat", function () {
      renderConnectionStatus();
      tickLiveMeta();
    });
    source.addEventListener("hub", function (event) {
      try { LIVE.lastEventAt = (JSON.parse(event.data).ts) || LIVE.lastEventAt; } catch (error) {}
      pulseBeacon();
      syncDelta("event");
    });
    source.addEventListener("reconnect", function () {
      disconnectLive();
      LIVE.reconnectTimer = setTimeout(connectLive, 300);
    });
    source.onerror = function () {
      disconnectLive();
      LIVE.failures += 1;
      renderConnectionStatus();
      startFallback();
      LIVE.reconnectTimer = setTimeout(connectLive, Math.min(10000, 900 + LIVE.failures * 700));
    };
  }
  function pulseBeacon() {
    // A visible confirmation that the stream is carrying traffic RIGHT NOW, independent of
    // whether the payload happened to change anything this page is showing.
    flashClass(doc.getElementById("statusPill"), "beat", 700);
  }
  function startLive() {
    tickLiveMeta();
    connectLive();
    LIVE.integrityTimer = setInterval(function () {
      if (!doc.hidden) syncSnapshot("integrity");
    }, 30000);
    setInterval(tickRelativeTimes, 5000);
    doc.addEventListener("visibilitychange", function () {
      if (doc.hidden) { disconnectLive(); stopFallback(); setStatus("paused", "Paused"); }
      else { syncSnapshot("visible"); connectLive(); }
    });
    global.addEventListener("beforeunload", disconnectLive);
  }

  /* ============================ LOCAL WORKER LAUNCH ============================ */
  // External protocols must be followed during the original user gesture. A fetch inside the
  // click handler loses that activation in some browsers, so controls are armed AHEAD of time
  // with short-lived, single-use grants. A ready click remains an ordinary anchor navigation:
  // no popup, no write token in browser storage, and no asynchronous hop.
  function launchCfg() { return D.worker_launch || {}; }
  function launchBase(anchor) {
    var protocol = String(launchCfg().protocol || "hub-worker").replace(/[^a-z0-9+.-]/g, "");
    if (protocol.indexOf("hub-") !== 0 && protocol.indexOf("-worker") < 0) protocol = "hub-worker";
    var task = anchor.getAttribute("data-task") || "";
    return protocol + "://start" + (task ? "/" + encodeURIComponent(task) : "");
  }
  function launchReady(anchor) {
    return anchor.getAttribute("data-launch-ready") === "1" &&
      parseInt(anchor.getAttribute("data-launch-expires") || "0", 10) * 1000 > Date.now() + 5000;
  }
  function prepareLaunch(anchor) {
    if (!launchCfg().enabled) return Promise.resolve({ ok: false, message: "Worker launch is disabled" });
    if (launchReady(anchor)) return Promise.resolve({ ok: true });
    if (anchor._launchGrantRequest) return anchor._launchGrantRequest;
    var count = parseInt(anchor.getAttribute("data-count") || "1", 10) || 1;
    var task = anchor.getAttribute("data-task") || "";
    var csrf = (doc.querySelector('meta[name="csrf-token"]') || {}).content || "";
    var endpoint = launchCfg().grant_endpoint || "/hub/api/launch-grant";
    anchor.setAttribute("aria-busy", "true");
    var request = fetch(endpoint, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
      body: JSON.stringify({ action: "start", task: task, count: count })
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (payload) {
        return { response: response, payload: payload };
      });
    });
    anchor._launchGrantRequest = request.then(function (result) {
      var data = (result.payload || {}).data || {};
      if (!result.response.ok || !data.grant) {
        var error = (((result.payload || {}).errors || [])[0] || {}).msg;
        return { ok: false, message: "Launch authorization refused — " + (error || ("HTTP " + result.response.status)) };
      }
      var base = launchBase(anchor);
      anchor.setAttribute("data-launch-base", base);
      anchor.href = base + "?count=" + count + "&grant=" + encodeURIComponent(data.grant);
      anchor.setAttribute("data-launch-ready", "1");
      anchor.setAttribute("data-launch-expires", String(data.expires || 0));
      return { ok: true };
    }, function () {
      return { ok: false, message: "Launch authorization failed — could not reach the Hub" };
    }).then(function (result) {
      anchor.removeAttribute("aria-busy");
      anchor._launchGrantRequest = null;
      return result;
    });
    return anchor._launchGrantRequest;
  }
  function launchClick(anchor, event) {
    if (launchReady(anchor)) {
      // Preserve the user activation: do not preventDefault and do not await anything here.
      var base = anchor.getAttribute("data-launch-base") || launchBase(anchor);
      var count = parseInt(anchor.getAttribute("data-count") || "1", 10) || 1;
      anchor.removeAttribute("data-launch-ready");
      anchor.removeAttribute("data-launch-expires");
      watchForWorker(count);
      setTimeout(function () { anchor.href = base; prepareLaunch(anchor); }, 0);
      return;
    }
    event.preventDefault();
    prepareLaunch(anchor).then(function (result) {
      toast(result.ok ? "Launch is authorized — click once more to open the local worker" : result.message,
            result.ok ? "info" : "error");
    });
  }
  function watchForWorker(count) {
    // A launch is a HAND-OFF to a process outside the browser, and the board cannot see whether
    // it started. So it watches its own canonical signal — a new live lease appearing — and says
    // so either way rather than leaving the operator staring at an unchanged page.
    var before = (live().inflight || []).length;
    var deadline = Date.now() + 45000;
    setStatus("connecting", "Waiting for worker");
    var timer = setInterval(function () {
      var now = (live().inflight || []).length;
      if (now > before) {
        clearInterval(timer);
        toast(count > 1 ? "Workers are on the board — leases claimed." : "Worker is on the board — lease claimed.", "success");
        flashClass(doc.getElementById("fleetCard"), "bumped", 1400);
        if (LIVE.connected) setStatus("live", "Live");
        return;
      }
      if (Date.now() > deadline) {
        clearInterval(timer);
        if (LIVE.connected) setStatus("live", "Live");
        toast("No lease appeared in 45s. Register the worker protocol handler, or check that the queue has ready work.", "error");
      }
    }, 1500);
  }
  function primeLaunchControls() {
    if (!launchCfg().enabled) return;
    [].forEach.call(doc.querySelectorAll("[data-launch]"), function (anchor) {
      if (anchor._launchPrimed) return;
      anchor._launchPrimed = true;
      anchor.hidden = false;
      anchor.href = launchBase(anchor);
      ["pointerenter", "focusin", "touchstart"].forEach(function (name) {
        anchor.addEventListener(name, function () { prepareLaunch(anchor); }, { passive: true });
      });
    });
  }
  function initLaunchControls() {
    primeLaunchControls();
    doc.addEventListener("click", function (event) {
      var anchor = event.target && event.target.closest ? event.target.closest("[data-launch]") : null;
      if (anchor) launchClick(anchor, event);
    });
  }

  /* ============================ TABS ============================ */
  var _panes = {};
  function activate(key) {
    TABS.forEach(function (t) {
      var on = t.key === key;
      if (t._btn) {
        t._btn.classList.toggle("active", on);
        t._btn.setAttribute("aria-selected", on ? "true" : "false");
        t._btn.setAttribute("tabindex", on ? "0" : "-1");
      }
      if (_panes[t.key]) _panes[t.key].classList.toggle("active", on);
    });
    try { var u = new URL(location.href); u.searchParams.set("tab", key); history.replaceState(null, "", u.pathname + u.search + location.hash); } catch (e) {}
  }

  function tabKeydown(event, key) {
    var index = TABS.findIndex(function (tab) { return tab.key === key; });
    var next = index;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (index + 1) % TABS.length;
    else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = (index - 1 + TABS.length) % TABS.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = TABS.length - 1;
    else return;
    event.preventDefault();
    activate(TABS[next].key);
    TABS[next]._btn.focus();
  }

  function build() {
    doc.body.classList.add("hub-app");
    var bm = doc.getElementById("brandMark"); if (bm) bm.appendChild(icon("cube"));
    var rb = doc.getElementById("refreshIco"); if (rb) rb.appendChild(icon("refresh"));
    var tabsBar = doc.getElementById("tabsBar"), panes = doc.getElementById("tabPanes");
    if (!tabsBar || !panes) return;
    TABS.forEach(function (t) {
      var btn = el("button", { class: "tab-btn", id: "tab-btn-" + t.key, type: "button", role: "tab",
        "data-tab": t.key, "aria-controls": "tab-" + t.key, "aria-selected": "false", tabindex: "-1" },
        [icon(t.icon), doc.createTextNode(" " + t.label)]);
      if (t.rows) { t._badge = el("span", { class: "tab-badge", text: String(t.rows.length) }); btn.appendChild(t._badge); }
      btn.addEventListener("click", function () { activate(t.key); });
      btn.addEventListener("keydown", function (event) { tabKeydown(event, t.key); });
      t._btn = btn; tabsBar.appendChild(btn);
      var pane = t.build ? t.build(t) : buildTableTab(t);
      _panes[t.key] = pane; panes.appendChild(pane);
    });
    var refresh = doc.getElementById("refreshBtn");
    if (refresh) refresh.addEventListener("click", function () { syncSnapshot("manual"); });
    initLaunchControls();
    var mo = doc.getElementById("universalModal");
    if (mo) mo.addEventListener("click", function (e) { if (e.target === mo) closeModal(); });
    var mc = doc.getElementById("modalClose"); if (mc) mc.addEventListener("click", closeModal);
    doc.addEventListener("keydown", function (e) { if (e.key === "Escape") closeModal(); });
    tickClock(); setInterval(tickClock, 1000);
    paintBackdrop();
    setStatus("connecting", "Connecting");

    global.HubCommands = [
      { id: "cmd:overview", title: "Go to Overview", sub: "tab", run: function () { activate("overview"); } },
      { id: "cmd:tasks", title: "Go to Tasks", sub: "tab", run: function () { activate("tasks"); } },
      { id: "cmd:fleet", title: "Show the fleet", sub: "verb", run: function () { focusCard("fleetCard"); } },
      { id: "cmd:attention", title: "What needs the operator?", sub: "verb", run: function () { focusCard("attentionCard"); } },
      { id: "cmd:adherence", title: "Board adherence", sub: "verb", run: function () { focusCard("adherenceCard"); } },
      { id: "cmd:dag", title: "Dependency frontier", sub: "verb", run: function () { focusCard("dagCard"); } },
      { id: "cmd:theme", title: "Toggle light / dark theme", sub: "verb", run: toggleTheme },
      { id: "cmd:density", title: "Cycle density", sub: "verb", run: cycleDensity },
      { id: "cmd:copy-link", title: "Copy deep link", sub: "verb", run: function () { try { navigator.clipboard.writeText(location.href); toast("Link copied", "success"); } catch (e) { toast("Copy failed", "error"); } } },
      { id: "cmd:refresh", title: "Sync now", sub: "verb", run: function () { syncSnapshot("manual"); } }
    ];

    var initial = "overview";
    try { var p = new URL(location.href).searchParams.get("tab"); if (p && _panes[p]) initial = p; } catch (e) {}
    var keyMap = { task: "tasks", adr: "adrs", feat: "feats", gap: "gaps", cap: "caps", deploy: "deploys", note: "notes" };
    if (location.hash) {
      var m = location.hash.slice(1).match(/^([a-z]+)-(.+)$/);
      if (m && keyMap[m[1]]) initial = keyMap[m[1]];
    }
    activate(initial);
    if (location.hash) {
      var hm = location.hash.slice(1).match(/^([a-z]+)-(.+)$/);
      if (hm && keyMap[hm[1]]) {
        var rec = null;
        Object.keys(BY_ID).forEach(function (k) { if (k.split(":")[1] === hm[1] && localId(k) === hm[2]) rec = BY_ID[k]; });
        if (rec) setTimeout(function () { openEntity(hm[1], rec); }, 60);
      }
    }
    startLive();
  }

  function toggleTheme() {
    var cur = (global.HubTheme && global.HubTheme.get && global.HubTheme.get()) || "system";
    var next = cur === "dark" ? "light" : "dark";
    if (global.HubTheme && global.HubTheme.set) global.HubTheme.set(next);
    else doc.documentElement.setAttribute("data-theme", next);
    toast("Theme: " + next, "info");
  }
  function cycleDensity() {
    var r = doc.documentElement, cur = r.getAttribute("data-density") || "comfortable";
    var next = cur === "compact" ? "comfortable" : "compact";
    r.setAttribute("data-density", next); toast("Density: " + next, "info");
  }

  global.Hub = { toast: toast, setStatus: setStatus, activate: activate, openEntity: openEntity,
                 closeModal: closeModal, sync: syncSnapshot, live: function () { return LIVE; } };

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", build);
  else build();
})(typeof window !== "undefined" ? window : this);
