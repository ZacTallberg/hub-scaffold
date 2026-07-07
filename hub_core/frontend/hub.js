/*
 * hub.js — hub client RENDERER (shared kit)
 * --------------------------------------------------
 * Renders the entire hub UI from the canonical <script id="hub-data"> JSON island
 * (the SAME payload /hub.json serves — UI == API by construction). Builds a tabbed
 * app shell: Overview + one tab per entity type, dense sortable/filterable tables,
 * row-click -> universal modal detail, toasts, a live clock/status pill, and command
 * palette verbs (via window.HubCommands, executed by palette.js).
 *
 * Contract: shell.css owns the look; palette.js owns Cmd-K. This file is pure DOM
 * (textContent — never innerHTML of snapshot text). Zero deps, zero CDN.
 */
(function (global) {
  "use strict";
  var doc = document;

  /* ---- inline icon set (Phosphor-ish, 24x24 stroke; static trusted markup) ---- */
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
    cube: '<path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>'
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
    s.innerHTML = P[name] || P.info;
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

  /* ---- data ---- */
  function parseData() {
    var e = doc.getElementById("hub-data");
    try { return JSON.parse(e.textContent || "{}"); } catch (x) { return {}; }
  }
  var D = parseData();
  var BY_ID = {};
  ["tasks", "adrs", "feats", "gaps", "caps", "deploys", "notes"].forEach(function (k) {
    (D[k] || []).forEach(function (r) { if (r && r.id) BY_ID[r.id] = r; });
  });

  /* ============================ TAB DEFINITIONS ============================ */
  var TABS = [
    { key: "overview", label: "Overview", icon: "gauge", build: buildOverview },
    { key: "tasks", label: "Tasks", icon: "checks", rows: D.tasks || [], type: "task", cols: COLS_TASK() },
    { key: "adrs", label: "ADRs", icon: "branch", rows: D.adrs || [], type: "adr", cols: COLS_ADR() },
    { key: "feats", label: "Features", icon: "package", rows: D.feats || [], type: "feat", cols: COLS_FEAT() },
    { key: "gaps", label: "Gaps", icon: "warning", rows: D.gaps || [], type: "gap", cols: COLS_GAP() },
    { key: "caps", label: "Capabilities", icon: "stack", rows: D.caps || [], type: "cap", cols: COLS_CAP() },
    { key: "deploys", label: "Deploys", icon: "rocket", rows: D.deploys || [], type: "deploy", cols: COLS_DEPLOY() },
    { key: "notes", label: "Findings", icon: "stack", rows: D.notes || [], type: "note", cols: COLS_NOTE() }
  ];

  /* column descriptors: {label, sort(optional key for value), cell(rec)->node, cls} */
  function txt(s, cls) { return el("td", cls ? { class: cls } : null, [doc.createTextNode(s == null ? "" : String(s))]); }
  function COLS_TASK() {
    return [
      { label: "ID", k: "legacy_ref", cls: "col-id", cell: function (r) { return txt(r.legacy_ref || localId(r.id), "col-id"); } },
      { label: "Status", k: "status", cell: function (r) { return el("td", null, [badge("task", r.status)]); } },
      { label: "Phase", k: "phase", cell: function (r) { return txt(r.phase, "cell-sub"); } },
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
  function COLS_DEPLOY() {
    return [
      { label: "At", k: "at", cls: "col-id", cell: function (r) { return txt(r.at, "col-id"); } },
      { label: "Build", k: "build", cell: function (r) { return txt(r.build); } },
      { label: "SHA", k: "sha", cls: "col-id", cell: function (r) { return txt(r.sha, "col-id"); } },
      { label: "Audit", k: "audit_ok", cell: function (r) { var ok = !!r.audit_ok; return el("td", null, [el("span", { class: "badge b-" + (ok ? "pass" : "fail") }, [el("span", { class: "b-glyph", "aria-hidden": "true", text: ok ? GLYPH.pass : GLYPH.fail }), doc.createTextNode(ok ? " ok" : " not ok")])]); } }
    ];
  }

  /* ============================ TABLE RENDER ============================ */
  function buildTableTab(tab) {
    var pane = el("div", { class: "tab-content", id: "tab-" + tab.key, role: "tabpanel" });
    // toolbar
    var search = el("input", { type: "search", placeholder: "Filter " + tab.label.toLowerCase() + "…", "aria-label": "Filter " + tab.label });
    var countEl = el("span", { class: "stat-value", text: String(tab.rows.length) });
    var toolbar = el("div", { class: "toolbar" }, [
      el("div", { class: "search-box" }, [icon("search", "s-icon"), search]),
      el("div", { class: "toolbar-spacer" }),
      el("div", { class: "stats-bar" }, [el("div", { class: "stat-item" }, [countEl, doc.createTextNode(" " + tab.label.toLowerCase())])])
    ]);
    // table
    var thead = el("tr");
    tab.cols.forEach(function (c, i) {
      var th = el("th", c.cls && c.cls.indexOf("num") >= 0 ? { class: "num sortable" } : { class: "sortable" }, [
        doc.createTextNode(c.label + " "), el("span", { class: "sort-ind", "aria-hidden": "true", text: "↕" })
      ]);
      th.addEventListener("click", function () { sortBy(tab, i); });
      th._col = c; thead.appendChild(th);
    });
    var tbody = el("tbody");
    var table = el("table", { class: "data-table" }, [el("thead", null, [thead]), tbody]);
    var wrap = el("div", { class: "table-wrapper" }, [table]);
    pane.append(toolbar, el("div", { class: "content-area" }, [el("div", { class: "full-table-view" }, [wrap])]));

    tab._tbody = tbody; tab._count = countEl; tab._thead = thead;
    renderRows(tab);
    search.addEventListener("input", function () { tab._q = search.value.trim().toLowerCase(); renderRows(tab); });
    return pane;
  }

  function renderRows(tab) {
    var tb = tab._tbody; tb.textContent = "";
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
      if (q) {
        var hay = [r.legacy_ref, r.title, r.name, r.summary, r.status, r.severity, r.maturity, r.phase, r.source, r.build, r.sha, localId(r.id)].join(" ").toLowerCase();
        if (hay.indexOf(q) < 0) return;
      }
      shown++;
      var tr = el("tr", { id: tab.type + "-" + localId(r.id), tabindex: "0", "data-hub-row": "", role: "button", "aria-label": (r.title || r.name || localId(r.id)) });
      tab.cols.forEach(function (c) { tr.appendChild(c.cell(r)); });
      tr.addEventListener("click", function () { openEntity(tab.type, r); });
      tr.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openEntity(tab.type, r); } });
      tr.addEventListener("focus", function () { activate(tab.key); }); // palette deep-link reveal
      tb.appendChild(tr);
    });
    if (!shown) {
      tb.appendChild(el("tr", null, [el("td", { colspan: tab.cols.length }, [
        el("div", { class: "empty-state" }, [icon("tray"), el("p", { text: q ? "No " + tab.label.toLowerCase() + " match “" + q + "” — clear the filter." : "No " + tab.label.toLowerCase() + " yet." })])
      ])]));
    }
    tab._count.textContent = String(shown);
  }

  function sortBy(tab, idx) {
    if (tab._sortIdx === idx) tab._sortDir = tab._sortDir === "asc" ? "desc" : "asc";
    else { tab._sortIdx = idx; tab._sortDir = "asc"; }
    [].forEach.call(tab._thead.children, function (th, i) {
      th.classList.toggle("sort-asc", i === idx && tab._sortDir === "asc");
      th.classList.toggle("sort-desc", i === idx && tab._sortDir === "desc");
      var ind = th.querySelector(".sort-ind");
      if (ind) ind.textContent = i === idx ? (tab._sortDir === "asc" ? "↑" : "↓") : "↕";
    });
    renderRows(tab);
  }

  /* ============================ OVERVIEW ============================ */
  function donut(pct, ok) {
    var r = 52, c = 2 * Math.PI * r, off = c * (1 - pct / 100);
    var ns = "http://www.w3.org/2000/svg";
    var s = doc.createElementNS(ns, "svg");
    s.setAttribute("viewBox", "0 0 128 128"); s.setAttribute("width", "128"); s.setAttribute("height", "128");
    s.setAttribute("role", "img"); s.setAttribute("aria-label", pct + "% of tasks done");
    function circle(cls, dash) {
      var ci = doc.createElementNS(ns, "circle");
      ci.setAttribute("cx", "64"); ci.setAttribute("cy", "64"); ci.setAttribute("r", String(r));
      ci.setAttribute("fill", "none"); ci.setAttribute("stroke-width", "12"); ci.setAttribute("class", cls);
      if (dash != null) { ci.setAttribute("stroke-dasharray", c.toFixed(1)); ci.setAttribute("stroke-dashoffset", dash.toFixed(1)); ci.setAttribute("stroke-linecap", "round"); ci.setAttribute("transform", "rotate(-90 64 64)"); }
      return ci;
    }
    s.appendChild(circle("d-track"));
    s.appendChild(circle("d-val" + (ok ? "" : " fail"), off));
    var t = doc.createElementNS(ns, "text"); t.setAttribute("x", "64"); t.setAttribute("y", "62"); t.setAttribute("text-anchor", "middle"); t.setAttribute("class", "d-center"); t.setAttribute("font-size", "26"); t.textContent = pct + "%";
    var t2 = doc.createElementNS(ns, "text"); t2.setAttribute("x", "64"); t2.setAttribute("y", "80"); t2.setAttribute("text-anchor", "middle"); t2.setAttribute("class", "d-sub"); t2.setAttribute("font-size", "11"); t2.textContent = "done";
    s.append(t, t2);
    return el("div", { class: "donut" }, [s]);
  }

  function kpi(n, label, jumpTab, tone) {
    var k = el("div", { class: "card card-accent kpi", tabindex: "0", role: "button" }, [
      el("div", { class: "kpi-n" + (tone ? " tone-" + tone : ""), text: String(n) }),
      el("div", { class: "kpi-l", text: label })
    ]);
    if (jumpTab) { var go = function () { activate(jumpTab); }; k.addEventListener("click", go); k.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } }); }
    return k;
  }

  function buildOverview() {
    var pane = el("div", { class: "tab-content", id: "tab-overview", role: "tabpanel" });
    var scroll = el("div", { class: "overview-scroll" });
    var ct = D.counts || {}, au = D.audit || {}, b = D.build || {};

    // hero: donut + KPIs
    var kpis = el("div", { class: "kpi-grid" }, [
      kpi((ct.done || 0) + "/" + (ct.total || 0), "tasks done", "tasks", "pass"),
      kpi(ct.in_progress || 0, "in progress", "tasks"),
      kpi(ct.blocked || 0, "blocked", "tasks", (ct.blocked ? "warn" : null)),
      kpi((D.gaps || []).filter(function (g) { return g.status !== "closed" && g.status !== "wont-fix"; }).length, "open gaps", "gaps", "fail"),
      kpi((D.feats || []).length, "features", "feats"),
      kpi((D.caps || []).length, "capabilities", "caps")
    ]);
    var hero = el("div", { class: "card" }, [el("div", { class: "card-body" }, [el("div", { class: "ov-hero" }, [
      donut(ct.pct || 0, au.ok), el("div", { class: "ov-hero-kpis" }, [kpis])
    ])])]);
    scroll.appendChild(hero);

    // audit card
    var auBody = el("div", { class: "card-body" });
    auBody.appendChild(el("p", { class: "cell-sub", style: "margin-bottom:12px",
      text: "Computed per request (never a cached boolean) · exit " + (au.exit_code) + " · critical " + ((au.counts || {}).critical || 0) + " · high " + ((au.counts || {}).high || 0) + " · warn " + ((au.counts || {}).warn || 0) }));
    if ((au.violations || []).length) {
      au.violations.slice(0, 12).forEach(function (v) {
        auBody.appendChild(el("div", { class: "callout " + (v.severity === "warn" ? "warn" : "fail") }, [
          el("span", { class: "b-glyph", "aria-hidden": "true", text: GLYPH[v.severity === "warn" ? "warn" : "fail"] }),
          el("div", null, [el("strong", { text: v.id + " " }), doc.createTextNode(v.observed || ""), v.remediation ? el("div", { class: "cell-sub", style: "margin-top:4px", text: "→ " + v.remediation }) : null])
        ]));
      });
    } else { auBody.appendChild(el("div", { class: "callout info" }, [el("span", { class: "b-glyph", "aria-hidden": "true", text: GLYPH.pass }), el("div", { text: "No violations — independently verified." })])); }
    var auCard = el("div", { class: "card" }, [
      el("div", { class: "card-header" }, [el("div", { class: "card-title" }, [icon("warning"), doc.createTextNode("Audit")]), el("span", { class: "badge b-" + (au.ok ? "pass" : "fail"), text: au.ok ? "PASS" : "FAIL" })]),
      auBody
    ]);

    // phases card
    var phBody = el("div", { class: "card-body" });
    (D.phases || []).forEach(function (p) {
      phBody.appendChild(el("div", { class: "phase-row" }, [
        el("span", { class: "phase-name", text: p.name }),
        el("div", { class: "phase-track" }, [el("div", { class: "phase-fill" + (p.pct >= 100 ? " full" : ""), style: "width:" + (p.pct || 0) + "%" })]),
        el("span", { class: "phase-pct", text: p.done + "/" + p.total })
      ]));
    });
    if (!(D.phases || []).length) phBody.appendChild(el("p", { class: "cell-sub", text: "No phases." }));
    var phCard = el("div", { class: "card" }, [el("div", { class: "card-header" }, [el("div", { class: "card-title" }, [icon("checks"), doc.createTextNode("Phase progress")])]), phBody]);

    // coherence card
    function ci(label, val, code) { return el("span", { class: "ci" }, [doc.createTextNode(label + " "), code ? el("code", { text: val == null ? "—" : String(val) }) : doc.createTextNode(val == null ? "—" : String(val))]); }
    var coh = !!b.coherent;
    var cohCard = el("div", { class: "card" }, [
      el("div", { class: "card-header" }, [el("div", { class: "card-title" }, [icon("rocket"), doc.createTextNode("Build coherence")]),
        el("span", { class: "badge b-" + (b.coherent === true ? "pass" : (b.coherent === false ? "fail" : "stale")), text: b.coherent === true ? "coherent" : (b.coherent === false ? "drift" : "unverified") })]),
      el("div", { class: "card-body" }, [el("div", { class: "coherence-strip" }, [
        ci("repo", b.repo, true), ci("deploy", b.deploy, true), ci("stamped sha", b.sha, true), ci("HEAD", b.head, true), ci("served", b.served_sha, true)
      ])])
    ]);

    scroll.append(el("div", { class: "ov-grid" }, [auCard, phCard]), cohCard);
    pane.appendChild(scroll);
    return pane;
  }

  /* ============================ MODAL ============================ */
  function row(label, valNode) { return el("div", { class: "detail-row" }, [el("div", { class: "detail-label", text: label }), valNode.nodeType ? el("div", { class: "detail-value" }, [valNode]) : el("div", { class: "detail-value", text: String(valNode) })]); }
  function rowMono(label, val) { return el("div", { class: "detail-row" }, [el("div", { class: "detail-label", text: label }), el("div", { class: "detail-value mono", text: val == null ? "—" : String(val) })]); }
  function section(title, ic, rows) { return el("div", { class: "detail-section" }, [el("div", { class: "detail-section-title" }, [icon(ic), doc.createTextNode(title)])].concat(rows.filter(Boolean))); }
  function chip(type, id) {
    var rec = BY_ID[id]; var label = rec ? (rec.title || rec.name || localId(id)) : localId(id);
    var t = (id.split(":")[1]) || type;
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
    var title = r.title || r.name || ("ADR " + r.number) || localId(r.id);
    var sub = r.id;
    var body = el("div");

    // identity + detail sections
    var idRows = [
      rowMono("ID", r.id),
      r.legacy_ref ? rowMono("Legacy", r.legacy_ref) : null,
      r.status ? row("Status", badge(type, r.status)) : null,
      r.severity ? row("Severity", el("span", { class: "sev-badge sev-" + r.severity, text: r.severity })) : null,
      r.maturity ? row("Maturity", badge("cap", r.maturity)) : null,
      r.phase ? row("Phase", r.phase) : null,
      r.number != null ? rowMono("Number", r.number) : null,
      r.version != null ? rowMono("Version", r.version) : null
    ];
    var detailRows = [
      r.summary ? row("Summary", r.summary) : null,
      r.source ? row("Source", r.source) : null,
      r.evidence ? row("Evidence", r.evidence) : null,
      r.needs ? row("Needs", r.needs) : null,
      r.pivot_notes ? row("Pivot", r.pivot_notes) : null,
      r.build ? rowMono("Build", r.build) : null,
      r.sha ? rowMono("SHA", r.sha) : null,
      r.at ? rowMono("At", r.at) : null,
      r.method ? row("Method", r.method) : null
    ];
    var grid = el("div", { class: "detail-grid" + (detailRows.filter(Boolean).length ? "" : " one") }, [section("Identity", "info", idRows)]);
    if (detailRows.filter(Boolean).length) grid.appendChild(section("Detail", iconName, detailRows));
    body.appendChild(grid);

    // links
    var links = [];
    if (r.tasks && r.tasks.length) links.push(row("Tasks", chipRow(r.tasks, "task")));
    if (r.deps && r.deps.length) links.push(row("Deps", chipRow(r.deps, "task")));
    if (r.addressed_by && r.addressed_by.length) links.push(row("Addressed by", chipRow(r.addressed_by, "task")));
    if (r.adrs && r.adrs.length) links.push(row("ADRs", chipRow(r.adrs, "adr")));
    if (r.superseded_by && r.superseded_by.length) links.push(row("Superseded by", chipRow(r.superseded_by, "adr")));
    if (r.verified_by && r.verified_by.length) links.push(row("Verified by", el("div", null, r.verified_by.map(function (s) { return el("div", { class: "detail-prose", text: "• " + s }); }))));
    if (links.length) body.appendChild(el("div", { class: "detail-grid one" }, [section("Links & evidence", "branch", links)]));

    // ADR prose
    ["context_md", "decision_md", "consequences_md"].forEach(function (f) {
      if (r[f]) body.appendChild(el("div", { class: "detail-grid one" }, [section(f.replace("_md", "").replace(/^./, function (c) { return c.toUpperCase(); }), "info", [el("div", { class: "detail-prose", text: r[f] })])]));
    });

    // provenance
    if (r.provenance) {
      var pv = r.provenance;
      body.appendChild(el("div", { class: "detail-grid one" }, [section("Provenance", "info", [
        rowMono("Created", pv.created_at), rowMono("Updated", pv.updated_at), pv.agent ? rowMono("Agent", pv.agent) : null,
        pv.commits && pv.commits.length ? rowMono("Commits", pv.commits.join(", ")) : null
      ])]));
    }
    openModal(role, title, sub, iconName, body);
    try { history.replaceState(null, "", "#" + type + "-" + localId(r.id)); } catch (e) {}
  }

  function openModal(role, title, subtitle, iconName, bodyNode) {
    var m = doc.getElementById("universalModal");
    var box = doc.getElementById("modalIcon"); box.className = "modal-icon t-" + role; box.textContent = ""; box.appendChild(icon(iconName));
    doc.getElementById("modalTitle").textContent = title;
    doc.getElementById("modalSubtitle").textContent = subtitle || "";
    var b = doc.getElementById("modalBody"); b.textContent = ""; b.appendChild(bodyNode); b.scrollTop = 0;
    m.classList.add("show");
    var c = doc.getElementById("modalClose"); if (c) c.focus();
  }
  function closeModal() { var m = doc.getElementById("universalModal"); if (m) m.classList.remove("show"); }

  /* ============================ TOAST + STATUS ============================ */
  var TOAST_ICON = { success: "check", error: "xc", info: "info" };
  function toast(message, type) {
    type = type || "info";
    var wrap = doc.getElementById("toastContainer"); if (!wrap) return;
    var t = el("div", { class: "toast " + type }, [icon(TOAST_ICON[type] || "info"), el("span", { text: message })]);
    wrap.appendChild(t);
    setTimeout(function () { t.classList.add("hiding"); setTimeout(function () { t.remove(); }, 300); }, 3800);
  }
  function setStatus(state, text) {
    var p = doc.getElementById("statusPill"); if (!p) return;
    p.className = "status-pill" + (state ? " " + state : "");
    var s = doc.getElementById("statusText"); if (s) s.textContent = text;
  }
  function tickClock() { var c = doc.getElementById("clock"); if (c) { var d = new Date(); c.textContent = d.toTimeString().slice(0, 8); } }

  /* ============================ TABS ============================ */
  var _panes = {};
  function activate(key) {
    TABS.forEach(function (t) {
      var on = t.key === key;
      if (t._btn) { t._btn.classList.toggle("active", on); t._btn.setAttribute("aria-selected", on ? "true" : "false"); }
      if (_panes[t.key]) _panes[t.key].classList.toggle("active", on);
    });
    try { var u = new URL(location.href); u.searchParams.set("tab", key); history.replaceState(null, "", u.pathname + u.search + location.hash); } catch (e) {}
  }

  function build() {
    doc.body.classList.add("hub-app");
    // brand mark icon
    var bm = doc.getElementById("brandMark"); if (bm) bm.appendChild(icon("cube"));
    var rb = doc.getElementById("refreshIco"); if (rb) rb.appendChild(icon("refresh"));
    var tabsBar = doc.getElementById("tabsBar"), panes = doc.getElementById("tabPanes");
    if (!tabsBar || !panes) return;
    TABS.forEach(function (t) {
      var btn = el("button", { class: "tab-btn", role: "tab", "data-tab": t.key, "aria-selected": "false" }, [icon(t.icon), doc.createTextNode(" " + t.label)]);
      if (t.rows) btn.appendChild(el("span", { class: "tab-badge", text: String(t.rows.length) }));
      btn.addEventListener("click", function () { activate(t.key); });
      t._btn = btn; tabsBar.appendChild(btn);
      var pane = t.build ? t.build() : buildTableTab(t);
      _panes[t.key] = pane; panes.appendChild(pane);
    });
    // refresh
    var refresh = doc.getElementById("refreshBtn");
    if (refresh) refresh.addEventListener("click", function () { setStatus("scanning", "Reloading…"); location.reload(); });
    // modal close wiring
    var mo = doc.getElementById("universalModal");
    if (mo) mo.addEventListener("click", function (e) { if (e.target === mo) closeModal(); });
    var mc = doc.getElementById("modalClose"); if (mc) mc.addEventListener("click", closeModal);
    doc.addEventListener("keydown", function (e) { if (e.key === "Escape") closeModal(); });
    // clock
    tickClock(); setInterval(tickClock, 1000);
    setStatus(D.audit && D.audit.ok ? "" : "error", D.audit && D.audit.ok ? "Healthy" : "Audit failing");

    // command palette verbs
    global.HubCommands = [
      { id: "cmd:overview", title: "Go to Overview", sub: "tab", run: function () { activate("overview"); } },
      { id: "cmd:open-gaps", title: "Filter: open gaps", sub: "tab", run: function () { activate("gaps"); } },
      { id: "cmd:theme", title: "Toggle light / dark theme", sub: "verb", run: toggleTheme },
      { id: "cmd:density", title: "Cycle density", sub: "verb", run: cycleDensity },
      { id: "cmd:audit", title: "Open audit", sub: "verb", run: function () { activate("overview"); } },
      { id: "cmd:copy-link", title: "Copy deep link", sub: "verb", run: function () { try { navigator.clipboard.writeText(location.href); toast("Link copied", "success"); } catch (e) { toast("Copy failed", "error"); } } },
      { id: "cmd:refresh", title: "Refresh", sub: "verb", run: function () { location.reload(); } }
    ];

    // deep link: ?tab= or #type-local
    var initial = "overview";
    try { var p = new URL(location.href).searchParams.get("tab"); if (p && _panes[p]) initial = p; } catch (e) {}
    if (location.hash) {
      var h = location.hash.slice(1), m = h.match(/^([a-z]+)-(.+)$/);
      if (m) { var keyMap = { task: "tasks", adr: "adrs", feat: "feats", gap: "gaps", cap: "caps", deploy: "deploys" }; if (keyMap[m[1]]) initial = keyMap[m[1]]; }
    }
    activate(initial);
    // open a deep-linked entity modal
    if (location.hash) {
      var hm = location.hash.slice(1).match(/^([a-z]+)-(.+)$/);
      if (hm) { var tm = { task: "task", adr: "adr", feat: "feat", gap: "gap", cap: "cap", deploy: "deploy" }[hm[1]];
        // ids are project:type:local; find by suffix
        var rec = null; Object.keys(BY_ID).forEach(function (k) { if (k.split(":")[1] === tm && localId(k) === hm[2]) rec = BY_ID[k]; });
        if (rec) setTimeout(function () { openEntity(tm, rec); }, 60);
      }
    }
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

  // public
  global.Hub = { toast: toast, setStatus: setStatus, activate: activate, openEntity: openEntity, closeModal: closeModal };

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", build);
  else build();
})(typeof window !== "undefined" ? window : this);
