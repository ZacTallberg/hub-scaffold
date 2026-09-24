/*
 * hub.js — hub client RENDERER (shared kit)
 * --------------------------------------------------
 * Renders the entire hub UI from the canonical <script id="hub-data"> JSON island (the SAME
 * payload /hub.json serves — UI == API by construction), then keeps it LIVE: SSE carries an
 * ordered canonical patch straight into the renderer. HTTP delta/snapshot reads exist only to
 * recover a cursor gap after reconnect. Animation is never allowed to become a second source of
 * truth.
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
    cap: { extracted: "pass", reusable: "pass", proven: "pass", prototype: "warn", concept: "info", service: "info" },
    directive: { active: "info", fulfilled: "pass", superseded: "stale", expired: "warn" }
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
  var TASK_SLE_H = { P0: 4, P1: 24, P2: 72, P3: 168 };
  function taskAgeSle(task) {
    var p = (task && task.provenance) || {};
    var created = Date.parse(p.created_at || "");
    if (isNaN(created)) return null;
    var terminal = task.status === "done" || task.status === "dropped";
    var ended = terminal ? Date.parse(p.updated_at || "") : Date.now();
    if (isNaN(ended)) ended = Date.now();
    var ageS = Math.max(0, Math.round((ended - created) / 1000));
    var hours = TASK_SLE_H[task.priority] || TASK_SLE_H.P2;
    var ratio = ageS / (hours * 3600);
    var role = ratio >= 1 ? "fail" : ratio >= .75 ? "warn" : "pass";
    var state = terminal ? (ratio >= 1 ? "closed beyond SLE" : "closed within SLE")
      : ratio >= 1 ? "SLE breached" : ratio >= .75 ? "SLE at risk" : "within SLE";
    return { age_s: ageS, hours: hours, ratio: ratio, role: role, state: state, terminal: terminal };
  }
  function taskAgeSleNode(task, compact) {
    var s = taskAgeSle(task);
    if (!s) return null;
    var ageLabel = s.terminal ? "cycle " + fmtAge(s.age_s) : "age " + fmtAge(s.age_s);
    var text = compact ? (ageLabel + " · " + s.state) : (ageLabel + " · " + s.hours + "h SLE · " + s.state);
    return el("span", { class: "task-age-sle b-" + s.role, text: text,
      "data-task-age-sle": task.id,
      title: (s.ratio * 100).toFixed(0) + "% of the " + s.hours + " hour service-level expectation" });
  }

  /* ---- data ---- */
  function parseData() {
    var e = doc.getElementById("hub-data");
    try { return JSON.parse(e.textContent || "{}"); } catch (x) { return {}; }
  }
  var D = parseData();
  var BY_ID = {};
  var COLLECTIONS = ["tasks", "adrs", "feats", "gaps", "caps", "deploys", "notes",
                     "directives", "acks"];
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
    { key: "notes", label: "Findings", icon: "stack", pick: function (d) { return d.notes || []; }, type: "note", cols: COLS_NOTE() },
    { key: "directives", label: "Directives", icon: "bolt", pick: function (d) { return d.directives || []; }, type: "directive", cols: COLS_DIRECTIVE() }
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
  function completionProof(task) {
    var command = String((task && task.verification_command) || "").trim();
    var receipt = receiptOf(task);
    var passed = !!(receipt && receipt.exit_code === 0);
    return {
      command: command,
      declared: !!command,
      receipt: receipt,
      passed: passed,
      complete: !command || passed
    };
  }
  // The same definition hub_core.checkpoints uses: a scheduler's lifecycle row and a grown
  // placeholder are shown in the plan but never counted toward "N of N done".
  var LIFECYCLE_KINDS = { handed_back: 1, lease_released: 1, reaped: 1, launcher_timeout: 1,
                          claim_expired: 1, lifecycle: 1 };
  function isWorkStep(s) {
    if (!s || typeof s !== "object") return false;
    if (s.lifecycle === true || LIFECYCLE_KINDS[s.kind]) return false;
    if (!s.done && !String(s.note || "").trim() &&
        (s.auto === true || /^step \d+$/i.test(String(s.step || "").trim()))) return false;
    return true;
  }
  // Each checkpoint as the worker recorded it — typed kind, the commit and pipeline it names,
  // the note — with scheduler rows and grown placeholders labelled for what they are.
  function checkpointRows(task) {
    return ((task && task.plan) || []).map(function (s, i) {
      if (!s || typeof s !== "object") return null;
      var kind = s.kind || "checkpoint";
      var label = (i + 1) + ". " + (s.done ? "✓ " : "○ ") + (s.step || "");
      var meta = [];
      if (!isWorkStep(s)) meta.push(s.auto ? "placeholder — not counted" : "scheduler row — not counted");
      if (kind !== "checkpoint") meta.push(kind + (s.times > 1 ? " ×" + s.times : ""));
      if (s.sha) meta.push("commit " + s.sha);
      if (s.pipeline_id) meta.push("pipeline " + s.pipeline_id);
      if (s.note_at) meta.push(relativeTime(s.note_at));
      return row(label, el("div", null, [
        s.note ? el("div", { class: "detail-prose", text: s.note }) : null,
        meta.length ? el("div", { class: "cell-sub mono", text: meta.join(" · ") }) : null,
        s.pipeline_url && /^https?:\/\//.test(s.pipeline_url)
          ? el("a", { class: "cell-sub", href: s.pipeline_url, rel: "noopener noreferrer", target: "_blank",
                      text: "open pipeline" }) : null
      ].filter(Boolean)));
    }).filter(Boolean);
  }
  function taskProgress(task) {
    var plan = ((task && task.plan) || []).filter(isWorkStep);
    if (!plan.length) return null;
    var done = plan.filter(function (s) { return s && s.done; }).length;
    return { done: done, total: plan.length, pct: Math.round(done * 100 / plan.length),
             step: (plan.filter(function (s) { return s && !s.done; })[0] || {}).step || null };
  }
  function taskStatusBadge(task) {
    // `done` means the real operation completed. A receipt is required only when this task
    // explicitly declared a rare critical-boundary probe; ordinary work is never downgraded for
    // correctly having no verification artifact.
    var b = badge("task", task.status);
    if (task.status !== "done") return b;
    var proof = completionProof(task), r = proof.receipt;
    if (proof.passed) {
      b.setAttribute("title", "Completed · critical probe passed: " + (r.command || proof.command) + " → exit 0");
    } else if (proof.declared) {
      b.setAttribute("title", r ? ("Completed · declared critical probe recorded exit " + r.exit_code)
                                : "Completed · declared critical probe has no recorded receipt");
      b.className = "badge b-warn";
    } else {
      b.setAttribute("title", "Completed through the real operation · no separate critical probe declared");
    }
    return b;
  }

  /* column descriptors: {label, k (sort key), cell(rec)->node, cls} */
  function txt(s, cls) { return el("td", cls ? { class: cls } : null, [doc.createTextNode(s == null ? "" : String(s))]); }
  function COLS_TASK() {
    return [
      { label: "Task", k: "title", cls: "col-title", cell: function (r) { return el("td", { class: "col-title" }, [
          el("span", { class: "task-title", text: r.title || localId(r.id) }), taskAgeSleNode(r, false)
        ].filter(Boolean)); } },
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
        } }
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
  function COLS_DIRECTIVE() {
    function ackCount(r) {
      var n = 0;
      (D.acks || []).forEach(function (a) { if (a.directive === r.id) n++; });
      return n;
    }
    return [
      { label: "ID", k: "id", cls: "col-id", cell: function (r) { return txt(localId(r.id), "col-id"); } },
      { label: "Status", k: "status", cell: function (r) { return el("td", null, [badge("directive", r.status)]); } },
      { label: "Title", k: "title", cls: "col-title", cell: function (r) { return txt(r.title, "col-title"); } },
      { label: "Targets", k: "targets", cell: function (r) { return txt((r.targets || []).join(", "), "cell-sub"); } },
      { label: "Acked", k: "id", cls: "num", sortVal: ackCount, cell: function (r) {
          var named = (r.targets || []).filter(function (t) { return t !== "all"; }).length;
          return txt(named ? (ackCount(r) + "/" + named) : String(ackCount(r)), "num");
        } },
      { label: "Deadline", k: "deadline", cell: function (r) {
          if (!r.deadline) return txt("", "cell-sub");
          var overdue = r.status === "active" && Date.parse(r.deadline) < Date.now();
          return el("td", null, [el("span", { class: overdue ? "deadline-overdue" : "cell-sub",
            text: (overdue ? "overdue · " : "") + relativeTime(r.deadline) })]);
        } },
      { label: "Kind", k: "answers", cell: function (r) { return txt(r.answers ? "answer" : "directive", "cell-sub"); } }
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
                      caps: "maturity", deploys: "audit_ok", notes: "category",
                      directives: "status" };
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
        "aria-pressed": on ? "true" : "false", "data-focus-key": "facet:" + tab.key + ":" + v }, [
        doc.createTextNode(v + " "), el("span", { class: "facet-n", text: String(counts[v]) })
      ]);
      chip.addEventListener("click", function () { setFacet(tab.key, v); });
      tab._facetBar.appendChild(chip);
    });
    if (tab._facet) {
      var clear = el("button", { class: "facet-chip is-clear", type: "button", text: "clear",
        "data-focus-key": "facet:" + tab.key + ":clear" });
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
      el("div", { class: "tcard-title", text: task.title || localId(task.id) }),
      taskAgeSleNode(task, true),
      el("div", { class: "tcard-head" }, [
        el("span", { class: "tcard-id", text: task.legacy_ref || localId(task.id) }),
        taskStatusBadge(task),
        lease ? el("span", { class: "tcard-agent" + (lease.stalled ? " is-stalled" : ""),
                             title: "held " + fmtAge(lease.age_s) }, [
          el("span", { class: "lease-dot", "aria-hidden": "true" }),
          doc.createTextNode(lease.agent || "worker")
        ]) : null
      ].filter(Boolean))
    ].filter(Boolean);
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
        var hay = [r.legacy_ref, r.title, r.name, r.summary, r.status, r.severity, r.maturity, r.phase, r.source, r.build, r.sha, (r.targets || []).join(" "), localId(r.id)].join(" ").toLowerCase();
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
    if (event.event === "task.transitioned") {
      return event.status === "done" ? "Task completed"
        : event.status === "in_progress" ? "Task started"
        : event.status === "blocked" ? "Task blocked"
        : event.status === "canceled" ? "Task canceled"
        : event.status === "todo" ? "Task returned to the queue"
        : "Task state changed";
    }
    var labels = {
      "task.created": "Task entered the system",
      "task.updated": "Task progress changed",
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
    "open-question": "Open question", "error-unclaimed": "Unclaimed error",
    "stuck-question": "Stuck question", "gate": "Needs a person", "slow-route": "Slow route",
    "delivery-unmeasured-landing": "Landing unknown",
    "delivery-unmeasured-release": "Release unknown",
    "delivery-unmeasured-live": "Live state unknown"
  };
  var ATTN_TONE = {
    "board-drained": "fail", "stalled-lease": "warn", "dangling-dep": "warn",
    "governance-amber": "warn", "blocked": "info", "needs-spec": "info",
    "circuit-open": "fail", "adherence-drift": "warn", "unlanded": "warn",
    "open-question": "warn", "error-unclaimed": "fail",
    "stuck-question": "fail", "gate": "warn", "slow-route": "warn",
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
    } else if (it.route && it.route.focus === "asks") {
      node.addEventListener("click", function () { focusCard("asksCard"); });
    } else if (it.route && it.route.focus === "errors") {
      node.addEventListener("click", function () { focusCard("errorsCard"); });
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
      copy.push(el("span", { class: "activity-receipt",
        title: event.receipt.command || "Critical-probe receipt",
        text: "critical probe " + (event.receipt.exit_code === 0 ? "passed" : "recorded") +
          (event.receipt.ran_by ? " · " + event.receipt.ran_by : "") +
          " · exit " + event.receipt.exit_code }));
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
     The chart is explicitly a frontier histogram. It never draws a synthetic edge between an
     arbitrary dot in adjacent layers. The separately rendered longest chain is the actual path
     returned by hub_core.dag and names every task it shows. */
  function dagChart(dg) {
    // One column per dependency layer, explicitly labelled with its exact width. This is a
    // histogram of workable frontier mass, not a node/edge diagram.
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
      "aria-label": "Frontier histogram: " + n + " layers, widest " + dg.max_frontier_width
                    + ", critical path " + dg.critical_path_length });

    var stepX = W / (n + 1);
    var pts = layers.map(function (_w, i) { return stepX * (i + 1); });

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
        g.appendChild(svgEl("circle", { cx: x.toFixed(1), cy: y.toFixed(1), r: "4",
          class: "dag-node" }));
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
    cap.textContent = "frontier histogram · tasks workable at each step  →";
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
        var b = el("button", { class: "dag-step", type: "button", title: n.id,
          text: n.title || localId(n.id) });
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

  function deliveryCard(deliv) {
    if (!deliv || !deliv.counts) return null;
    var c = deliv.counts, measured = deliv.measured || {}, notes = deliv.notes || {};
    var legs = [
      { key: "done", label: "Completed", value: c.done, measured: true },
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

  function progressHero(P, rd, fleet, tel, cost, wipSt, attention) {
    P = P || {};
    var pct = P.pct || 0;
    var perHr = P.last_1h || 0;
    var ready = (rd && rd.ready) || 0;
    var working = (fleet || []).filter(function (c) { return c.status === "working"; }).length;
    var stalled = (fleet || []).filter(function (c) { return c.status === "stalled"; }).length;
    var attentionN = (attention || []).length;
    var etaMin = (perHr > 0 && ready > 0) ? Math.round(ready / (perHr / 60)) : null;
    var vel = "≈ " + perHr + " tasks/hr"
      + (working ? "  ·  " + working + (working === 1 ? " worker working" : " workers working") : "")
      + (stalled ? "  ·  " + stalled + " stalled" : "")
      + (ready ? "  ·  " + ready + " ready" + (etaMin != null ? " (≈ " + fmtAge(etaMin * 60) + " to drain)" : "") : "  ·  ready queue drained");
    if (wipSt && wipSt.ceiling) {
      vel += "  ·  WIP " + wipSt.active + "/" + wipSt.ceiling + (wipSt.saturated ? " (saturated)" : "");
    }
    var telLine = telemetryLine(tel, cost);
    var constraint = stalled ? stalled + (stalled === 1 ? " worker needs intervention" : " workers need intervention")
      : attentionN ? attentionN + (attentionN === 1 ? " operator decision is waiting" : " operator decisions are waiting")
      : ready ? ready + (ready === 1 ? " task is ready for pickup" : " tasks are ready for pickup")
      : working ? working + (working === 1 ? " worker is advancing the board" : " workers are advancing the board")
      : "The queue is clear and no work is waiting";
    var stats = el("div", { class: "progress-stats" }, [
      el("div", { class: "pstat" + (working ? " is-live" : "") }, [
        el("span", { class: "pstat-num", text: String(working) }), el("span", { class: "pstat-lbl", text: "active now" })]),
      el("div", { class: "pstat" + (ready ? " is-ready" : "") }, [
        el("span", { class: "pstat-num", text: String(ready) }), el("span", { class: "pstat-lbl", text: "ready next" })]),
      el("div", { class: "pstat" + (attentionN ? " is-alert" : "") }, [
        el("span", { class: "pstat-num", text: String(attentionN) }), el("span", { class: "pstat-lbl", text: "need attention" })]),
      el("div", { class: "pstat is-rate" }, [
        el("span", { class: "pstat-num", text: String(perHr) }), el("span", { class: "pstat-lbl", text: "completed / hour" })])
    ]);
    return el("section", { class: "card progress-hero" }, [
      el("div", { class: "hero-command" }, [
        el("div", { class: "hero-command-copy" }, [
          el("span", { class: "live-orb", "aria-hidden": "true" }),
          el("span", { class: "hero-command-kicker", text: "Flow state" }),
          el("strong", { text: constraint })
        ]),
        el("span", { class: "hero-command-mode", text: working ? "in motion" : ready ? "ready" : attentionN ? "decision" : "clear" })
      ]),
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
    // The card carries lists and progress bars — flow content, which a <button> may not contain:
    // wrapping it flattens every list into the button's accessible name and drops the structure
    // assistive tech navigates by. So the CARD stays an <article> and the agent NAME is the real
    // button, stretched over the card by CSS so the whole surface is still one click.
    var opener = el("button", { class: "agent-open", type: "button",
      "aria-label": "open agent " + c.agent + " — " + c.status + (c.task ? " on " + c.task : "") });
    opener.addEventListener("click", function () { openAgentDetail(c); });
    var head = el("div", { class: "agent-head" }, [
      el("span", { class: "agent-dot s-" + c.status, "aria-hidden": "true" }),
      el("span", { class: "agent-name", text: c.agent }),
      c.machine ? el("span", { class: "agent-mach", title: "machine", text: c.machine }) : null,
      el("span", { class: "agent-status s-" + c.status, text: c.status })
    ].filter(Boolean));
    var now = c.task
      ? el("div", { class: "agent-now" }, [
          el("span", { class: "agent-now-lbl", text: "on" }),
          el("span", { class: "agent-now-task", text: c.task }),
          el("span", { class: "agent-now-age", text: c.age_s != null ? fmtAge(c.age_s) : "" })
        ])
      : c.focus
      // No claim, but a live console with a focus: actively working something, just not a
      // board task. Show WHAT, not "idle" — the exact case that makes a busy fleet look asleep.
      ? el("div", { class: "agent-now" }, [
          el("span", { class: "agent-now-lbl", text: "on" }),
          el("span", { class: "agent-now-task", text: c.focus })
        ])
      : el("div", { class: "agent-now is-idle", text: c.idle_s != null ? ("last active " + fmtAge(c.idle_s) + " ago") : "idle" });
    var kids = [head, now];
    if (c.last_note) {
      // The last checkpoint note: the context that turns "working on X" into
      // "working on X, last did Y".
      kids.push(el("div", { class: "agent-note", text: "↳ " + c.last_note }));
    }
    // Every live console this agent has open (id · directory · focus): the surface that
    // stops two sessions from unknowingly working the same thing.
    if ((c.sessions || []).length) {
      kids.push(el("ul", { class: "agent-sessions" }, c.sessions.slice(0, 4).map(function (s) {
        // One console: its name (or id), the project it stands in, whether THIS console holds
        // a task (a claim is never inferred from a directory), its state and focus.
        return el("li", { class: "agent-sess" + (s.state === "working" ? " is-working" : "") }, [
          el("span", { class: "sess-id", text: s.name || s.session || "?" }),
          // Which agent runtime this console is (X-Hub-Runtime): two runtimes on one board
          // coordinate differently, so the row says which one a message will land in.
          s.runtime ? el("span", { class: "sess-rt", title: "agent runtime", text: s.runtime }) : null,
          s.project ? el("span", { class: "sess-cwd", title: s.cwd || "", text: s.project }) : (s.cwd ? el("span", { class: "sess-cwd", text: s.cwd }) : null),
          s.project ? el("span", { class: "badge " + (s.has_task ? "b-pass" : "b-warn"),
                                   title: s.has_task ? (s.task_title || s.task_id) : "this console holds no task for " + s.project,
                                   text: s.has_task ? "task" : "no task" }) : null,
          // Only the console that CLAIMED the task is marked as holding it; a sibling
          // window standing in the same directory is not its owner.
          s.task_id ? el("span", { class: "sess-held", title: "this console holds " + s.task_id,
                                   text: "holds " + localId(s.task_id) }) : null,
          s.focus ? el("span", { class: "sess-focus", text: s.focus }) : null,
          el("span", { class: "sess-age", text: (s.state ? s.state + " · " : "") + (s.age_s != null ? fmtAge(s.age_s) : "") })
        ].filter(Boolean));
      })));
    }
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
    // Every card opens the per-agent detail view — the coordination surface. The held
    // task stays one click away INSIDE it (a chip), so nothing their card answered before
    // is further away, and a claimless-but-active console finally has somewhere to open.
    return el("article", { class: "agent-card s-" + c.status,
      "data-seq": String((c.trail && c.trail[0] && c.trail[0].seq) || ""),
      "data-agent": c.agent }, [opener].concat(kids));
  }

  function workerHealthRow(h) {
    if (!h) return null;
    var parts = [h.seats_with_done_work + (h.seats_with_done_work === 1 ? " seat" : " seats") + " with completed work"];
    if (h.receipts) {
      parts.push("critical probes " + (h.receipts - h.failed) + "/" + h.receipts + " passed"
        + (h.failed ? " (" + h.failed + " needs attention)" : ""));
    } else {
      parts.push("no critical-probe receipts in the last " + h.window + " events — ordinary completions need none");
    }
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
    group("need spec", rd.needs_spec, rd.needs_spec_top, "warn", "unblocked executable work needs concrete acceptance");
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


  /* ---- ASKS: the question threads, not a count ----
     Derived client-side from the collections the snapshot already carries (question notes +
     answer directives + delivery acks), so the card patches live with the board. A question
     is a person blocked for a measurable time: open threads lead with their waiting age,
     answered ones show the reply as a turn plus whether delivery LANDED (the asker's ack) —
     "answered" and "delivered" are different facts. Lanes and a 14-day strip answer "are we
     keeping up" without counting rows. */
  var ANSWER_ECHO = "\n\n---\nIn answer to your question:";
  function askThreads() {
    var acksByDirective = {};
    (D.acks || []).forEach(function (a) {
      (acksByDirective[a.directive] = acksByDirective[a.directive] || []).push(
        String(a.agent || "").toLowerCase());
    });
    var answerByQuestion = {};
    (D.directives || []).forEach(function (d) {
      if (d.answers) answerByQuestion[d.answers] = d;
    });
    var threads = [];
    (D.notes || []).forEach(function (n) {
      var tags = (n.tags || []).map(function (t) { return String(t).toLowerCase(); });
      if (tags.indexOf("question") < 0) return;
      var asker = String(n.asker || (n.provenance || {}).agent || "").toLowerCase();
      var reply = answerByQuestion[n.id];
      var answerText = String((reply || {}).body_md || "");
      var echoAt = answerText.indexOf(ANSWER_ECHO);
      if (echoAt >= 0) answerText = answerText.slice(0, echoAt);
      var askedAt = (n.provenance || {}).created_at || "";
      var answeredAt = reply ? ((reply.provenance || {}).created_at || "") : "";
      var askedMs = Date.parse(askedAt), answeredMs = Date.parse(answeredAt);
      threads.push({
        id: n.id, asker: asker, title: n.title || "", context: n.body_md || "",
        to: String(n.to || "").toLowerCase(),
        gate: tags.indexOf("human-only") >= 0,
        open: tags.indexOf("open") >= 0,
        answered: !!reply,
        answer: answerText,
        answerBy: reply ? String((reply.provenance || {}).agent || "") : "",
        acked: !!(reply && (acksByDirective[reply.id] || []).indexOf(asker) >= 0),
        askedAt: askedAt, askedMs: isNaN(askedMs) ? null : askedMs,
        answeredMs: isNaN(answeredMs) ? null : answeredMs,
        waitS: null, replyS: null
      });
    });
    var now = Date.now();
    threads.forEach(function (t) {
      if (t.open && t.askedMs != null) t.waitS = Math.max(0, Math.round((now - t.askedMs) / 1000));
      if (t.answered && t.askedMs != null && t.answeredMs != null) {
        t.replyS = Math.max(0, Math.round((t.answeredMs - t.askedMs) / 1000));
      }
    });
    // Newest first, then OPEN threads LONGEST WAIT FIRST (an unknown age last): the ask that has
    // waited two days must lead the card, not sit under twenty fresher ones.
    threads.sort(function (a, b) { return (b.askedMs || 0) - (a.askedMs || 0); });
    threads.sort(function (a, b) {
      function rank(t) { return t.open ? 0 : (t.answered && !t.acked) ? 1 : 2; }
      var r = rank(a) - rank(b);
      if (r || !a.open) return r;
      return (a.waitS == null) - (b.waitS == null) || (b.waitS || 0) - (a.waitS || 0);
    });
    return threads;
  }
  function askThread(t) {
    var stateLbl = t.open ? (t.waitS != null ? "waiting " + fmtAge(t.waitS) : "waiting")
                 : !t.acked ? "answered — awaiting the asker's ack"
                 : "closed" + (t.replyS != null ? " · replied in " + fmtAge(t.replyS) : "");
    var kids = [
      el("span", { class: "ask-head" }, [
        el("span", { class: "ask-from", text: (t.asker || "someone") + (t.to ? " → " + t.to : "") }),
        t.gate ? el("span", { class: "badge b-warn", text: "needs a person" }) : null,
        t.stuck ? el("span", { class: "badge b-fail", text: "stuck" }) : null,
        el("span", { class: "ask-state" + (t.open ? " is-open" : t.acked ? " is-closed" : " is-answered"),
                     text: stateLbl }),
        el("time", { class: "rel-time ask-age", datetime: t.askedAt || "", "data-ts": t.askedAt || "",
                     text: relativeTime(t.askedAt) })
      ].filter(Boolean)),
      el("span", { class: "ask-title", text: t.title })
    ];
    if (t.context) kids.push(el("span", { class: "ask-body", text: String(t.context).slice(0, 220) }));
    if (t.answered && t.answer) {
      kids.push(el("span", { class: "ask-answer" }, [
        el("span", { class: "ask-answer-by", text: (t.answerBy || "the operator") + " replied" }),
        el("span", { class: "ask-answer-text", text: String(t.answer).slice(0, 300) })
      ]));
    }
    var node = el("button", { class: "ask-item" + (t.open ? "" : " is-settled"), type: "button",
      "data-focus-key": "ask:" + (t.id || t.title), "data-entity-id": t.id || null,
      "aria-label": (t.asker || "someone") + " asks " + t.title }, kids);
    if (t.id && BY_ID[t.id]) {
      node.addEventListener("click", function () { openEntity("note", BY_ID[t.id]); });
    } else { node.disabled = true; }
    return node;
  }
  function askStrip(threads) {
    // 14 days of asked (top, neutral) vs answered (bottom, pass) — keeping up is a SHAPE.
    var days = [];
    var now = Date.now();
    for (var back = 13; back >= 0; back--) {
      var lo = now - (back + 1) * 86400000, hi = now - back * 86400000;
      days.push({
        asked: threads.filter(function (t) { return t.askedMs != null && t.askedMs >= lo && t.askedMs < hi; }).length,
        answered: threads.filter(function (t) { return t.answeredMs != null && t.answeredMs >= lo && t.answeredMs < hi; }).length
      });
    }
    if (!days.some(function (d) { return d.asked || d.answered; })) return null;
    var max = Math.max.apply(null, [1].concat(days.map(function (d) { return Math.max(d.asked, d.answered); })));
    return el("div", { class: "ask-strip", "aria-hidden": "true",
                       title: "last 14 days — asked (top) vs answered (bottom)" },
      days.map(function (d) {
        return el("span", { class: "ask-strip-day" }, [
          el("span", { class: "ask-strip-asked", style: "height:" + Math.round((d.asked / max) * 12) + "px" }),
          el("span", { class: "ask-strip-answered", style: "height:" + Math.round((d.answered / max) * 12) + "px" })
        ]);
      }));
  }
  function asksCard() {
    var threads = askThreads();
    // STUCK is the server's threshold (live.asks_stuck), never a number re-derived here.
    var stuckInfo = live().asks_stuck || {};
    var stuckAfter = stuckInfo.stuck_after_seconds || 7200;
    threads.forEach(function (t) { t.stuck = !!(t.open && !t.answered && (t.waitS || 0) >= stuckAfter); });
    var open = threads.filter(function (t) { return t.open; });
    var stuck = open.filter(function (t) { return t.stuck; });
    var awaiting = threads.filter(function (t) { return t.answered && !t.acked && !t.open; });
    var closed = threads.length - open.length - awaiting.length;
    var body = el("div", { class: "card-body" });
    // Per-asker lanes: who is blocked, how badly — the rows that change a decision first.
    var lanes = {};
    open.forEach(function (t) {
      var lane = lanes[t.asker] = lanes[t.asker] || { agent: t.asker, open: 0, worst: 0 };
      lane.open += 1;
      lane.worst = Math.max(lane.worst, t.waitS || 0);
    });
    var laneRows = Object.keys(lanes).map(function (k) { return lanes[k]; })
      .sort(function (a, b) { return (b.worst - a.worst) || (b.open - a.open); });
    if (laneRows.length > 1) {
      body.appendChild(el("div", { class: "ask-lanes" }, laneRows.slice(0, 4).map(function (l) {
        return el("span", { class: "ask-lane", text:
          l.agent + " · " + l.open + " open · worst " + fmtAge(l.worst) });
      })));
    }
    if (stuck.length) {
      // The card LEADS with what is stuck and for how long — an ask whose age is invisible
      // cannot be triaged, and one past the threshold is a person blocked for hours.
      body.appendChild(el("div", { class: "ask-stuck", role: "status" }, [
        el("strong", { text: stuck.length + " stuck" }),
        doc.createTextNode(" — oldest " + fmtAge(stuck[0].waitS || 0) + " (waiting past " +
          fmtAge(stuckAfter) + "; asks past the unstick window reach every console)")
      ]));
    }
    if (!threads.length) {
      body.appendChild(el("div", { class: "attn-clear" }, [
        el("span", { class: "b-glyph", "aria-hidden": "true", text: GLYPH.pass }),
        doc.createTextNode(" No questions on the board — nobody is blocked waiting on an answer.")
      ]));
    } else {
      open.slice(0, 6).forEach(function (t) { body.appendChild(askThread(t)); });
      awaiting.slice(0, 3).forEach(function (t) { body.appendChild(askThread(t)); });
      if (!open.length && !awaiting.length) {
        body.appendChild(el("div", { class: "attn-clear" }, [
          el("span", { class: "b-glyph", "aria-hidden": "true", text: GLYPH.pass }),
          doc.createTextNode(" Every question is answered and delivered" +
            (closed ? " (" + closed + " closed)" : "") + ".")
        ]));
      } else if (closed) {
        body.appendChild(el("p", { class: "cell-sub", text: closed + " closed thread" + (closed === 1 ? "" : "s") + " not shown." }));
      }
      body.appendChild(el("p", { class: "cell-sub", style: "margin-top:8px", text:
        "Ask with the client (python -m hub_core.client ask); answering is ONE verb that replies to the asker AND retires the question." }));
    }
    var strip = askStrip(threads);
    if (strip) body.appendChild(strip);
    return el("section", { class: "card asks-card", id: "asksCard", "aria-labelledby": "asksTitle" }, [
      el("div", { class: "card-header" }, [
        el("div", { class: "card-title", id: "asksTitle" }, [icon("users"),
          doc.createTextNode("Questions" + (open.length ? "  ·  " + open.length + " open" : "") +
            (stuck.length ? "  ·  " + stuck.length + " stuck" : ""))]),
        stuck.length ? el("span", { class: "badge b-fail", text: stuck.length + " stuck · oldest " + fmtAge(stuck[0].waitS || 0) })
        : awaiting.length ? el("span", { class: "badge b-warn", text: awaiting.length + " undelivered" })
                        : el("span", { class: "badge b-" + (open.length ? "warn" : "pass"),
                                       text: open.length ? String(open.length) : "clear" })
      ]),
      body
    ]);
  }

  /* An error row's DETAILS (the stack trace, the context fields, the origin) are served
     with every row and were rendered nowhere — a queue that names a failure but withholds
     the way in sends the reader to a server shell. Not an entity, so it gets its own modal. */
  function openErrorDetail(r, liveRefresh) {
    var role = r.severity === "critical" ? "fail" : "warn";
    var body = el("div");
    var idRows = [
      row("Severity", el("span", { class: "badge b-" + role, text: r.severity || "error" })),
      rowMono("Source", r.source),
      rowMono("Fingerprint", r.fingerprint),
      r.origin ? rowMono("Origin", r.origin + (r.origin_app ? " · " + r.origin_app : "")
                                   + (r.origin_machine ? " · " + r.origin_machine : "")) : null,
      rowMono("At", r.ts),
      r.occurrences_since_last ? rowMono("Collapsed repeats",
        "\u00d7" + (1 + r.occurrences_since_last) + " (throttled; count preserved)") : null,
      r.acked ? row("Claimed", (r.acked.by || "someone") + (r.acked.note ? " — " + r.acked.note : ""))
              : row("Queue state", el("span", { class: "badge b-fail", text: "unclaimed" }))
    ];
    body.appendChild(el("div", { class: "detail-grid one" }, [section("Signature", "warning", idRows)]));
    body.appendChild(el("div", { class: "detail-grid one" }, [section("Message", "info", [
      el("div", { class: "detail-prose", text: r.message || "" })])]));
    var ctx = r.context || {};
    var ctxRows = Object.keys(ctx).map(function (k) { return rowMono(k, ctx[k]); });
    if (ctxRows.length) {
      body.appendChild(el("div", { class: "detail-grid one" }, [section("Context", "info", ctxRows)]));
    }
    if (r.details) {
      body.appendChild(el("div", { class: "detail-grid one" }, [section("Details", "info", [
        el("pre", { class: "err-details mono", text: r.details })])]));
    }
    body.appendChild(el("p", { class: "cell-sub", text:
      "Claim: POST api/ack-error {fingerprint: \"" + (r.fingerprint || "") + "\"} — collapses the signature off the queue without deleting rows." }));
    if (liveRefresh) {
      refreshModal(role, r.message || "Operational error", r.source || "", "warning", body);
      return;
    }
    // A non-entity dialog still has to obey the live contract: an error that gets CLAIMED
    // while its detail is open must stop reading "unclaimed" under the operator's eyes.
    _openModalLive = { kind: "error", key: r.fingerprint || r.source || "" };
    openModal(role, r.message || "Operational error", r.source || "", "warning", body);
  }

  /* The per-agent detail view — the coordination surface. A card answers "who is busy";
     this answers what a peer actually decides with: which consoles they have open and on
     what, the task they hold and its last checkpoint, their recent trail. */
  function openAgentDetail(c, liveRefresh) {
    var body = el("div");
    var idRows = [
      row("Status", el("span", { class: "agent-status s-" + c.status, text: c.status })),
      c.machine ? rowMono("Machine", c.machine) : null,
      c.done_total ? rowMono("Completed on this board", c.done_total) : null,
      c.idle_s != null ? rowMono("Last board action", fmtAge(c.idle_s) + " ago") : null
    ];
    body.appendChild(el("div", { class: "detail-grid one" }, [section("Seat", "users", idRows)]));
    if (c.task_id) {
      var held = [row("Task", chipRow([c.task_id], "task") || doc.createTextNode(c.task || ""))];
      if (c.plan_total) {
        held.push(row("Plan", el("div", null, [
          el("div", { class: "tcard-track" }, [el("div", { class: "tcard-fill",
            style: "width:" + (c.plan_pct || 0) + "%" })]),
          el("div", { class: "cell-sub", text: "step " + (c.plan_done || 0) + "/" + c.plan_total
             + (c.step ? " — " + c.step : "") })])));
      }
      if (c.last_note) held.push(row("Last checkpoint", c.last_note));
      if (c.age_s != null) held.push(rowMono("Held for", fmtAge(c.age_s)));
      body.appendChild(el("div", { class: "detail-grid one" }, [section("Holding", "checks", held)]));
    } else if (c.focus) {
      body.appendChild(el("div", { class: "detail-grid one" }, [section("Holding", "checks", [
        row("Ambient focus", c.focus),
        row("Note", "working, but not on a board task — coordinate before starting the same thing")])]));
    }
    if ((c.sessions || []).length) {
      var consoles = c.sessions.map(function (sess) {
        return row((sess.session || "?") + (sess.machine ? " @ " + sess.machine : ""),
          el("div", null, [
            sess.runtime ? el("span", { class: "sess-rt", title: "agent runtime", text: sess.runtime }) : null,
            sess.task_id ? el("div", { class: "cell-sub", text: "holds " + sess.task_id +
              " (claimed from this console)" }) : null,
            sess.focus ? el("div", { class: "detail-prose", text: sess.focus }) : null,
            el("div", { class: "cell-sub", text: (sess.cwd || "") +
               (sess.age_s != null ? "  ·  " + fmtAge(sess.age_s) + " ago" : "") })
          ].filter(Boolean)));
      });
      body.appendChild(el("div", { class: "detail-grid one" }, [section("Live consoles", "pulse", consoles)]));
    }
    if ((c.trail || []).length) {
      var trail = c.trail.slice(0, 5).map(function (t) {
        return row(t.action || "", el("div", null, [
          el("div", { class: "detail-prose", text: t.title || "" }),
          el("time", { class: "cell-sub rel-time", datetime: t.ts || "", "data-ts": t.ts || "",
                       text: relativeTime(t.ts) })]));
      });
      body.appendChild(el("div", { class: "detail-grid one" }, [section("Recent trail", "branch", trail)]));
    }
    var role = c.status === "stalled" ? "warn" : "info";
    if (liveRefresh) {
      refreshModal(role, c.agent, "agent", "users", body);
      return;
    }
    // The coordination surface must not freeze: a peer decides "are they still on this?" from
    // exactly this dialog, and a stale answer here is the duplicate-work bug it exists to stop.
    _openModalLive = { kind: "agent", key: c.agent };
    openModal(role, c.agent, "agent", "users", body);
  }

  /* ---- ERRORS: the operational stream the ledger audit cannot see ----
     The bar is applied at read and shared with the API, so the human and every machine
     consumer read the same queue. Deferred rows are counted, never hidden-and-forgotten. */
  function errorsCard(rows, meta) {
    rows = rows || []; meta = meta || {};
    var onBar = rows.filter(function (r) { return r.bar === "on"; });
    var body = el("div", { class: "card-body" });
    if (meta.available === false) {
      body.appendChild(el("div", { class: "callout fail" }, [
        el("span", { class: "b-glyph", "aria-hidden": "true", text: GLYPH.fail }),
        el("div", { text: "The error store is impaired (" + (meta.reason || "write failure") + ") — quiet here does NOT mean healthy." })
      ]));
    }
    var hourly = meta.hourly || [];
    if (hourly.length) {
      var max = Math.max.apply(null, [1].concat(hourly));
      body.appendChild(el("div", { class: "err-shape" }, [
        el("div", { class: "spark err-spark", "aria-hidden": "true" }, hourly.map(function (v) {
          return el("span", { class: "spark-bar" + (v ? "" : " is-zero"),
            style: "height:" + Math.round(4 + (v / max) * 22) + "px", title: v + " in that hour" });
        })),
        el("span", { class: "cell-sub", text:
          (meta.last_24h || 0) + " in 24h · " + (meta.trend || "quiet")
          + (meta.unclaimed ? " · " + meta.unclaimed + " unclaimed" : "")
          + (meta.external_rows ? " · " + meta.external_rows + " foreign" : "") })
      ]));
    }
    if (!onBar.length) {
      var cov = meta.coverage || {};
      var silent = (cov.channels || []).filter(function (c) { return c.silent; }).length;
      body.appendChild(el("div", { class: "attn-clear" }, [
        el("span", { class: "b-glyph", "aria-hidden": "true", text: GLYPH.pass }),
        doc.createTextNode(" Nothing on the board" +
          (silent ? " — but " + silent + " of " + (cov.channels || []).length +
            " reporting channels are silent, so read this as \u201cno report\u201d, not \u201cno failures\u201d." : "."))
      ]));
    } else {
      onBar.slice(0, 8).forEach(function (r) {
        var where = (r.context && r.context.app) || r.origin_app || r.origin_machine || r.origin || "";
        var item = el("button", { class: "err-item" + (r.acked ? " is-acked" : ""), type: "button",
          "data-focus-key": "err:" + (r.fingerprint || r.source || ""),
          "aria-label": "open error " + (r.message || "") }, [
          el("span", { class: "err-head" }, [
            el("span", { class: "badge b-" + (r.severity === "critical" ? "fail" : "warn"), text: r.severity || "error" }),
            where ? el("span", { class: "err-where", text: where }) : null,
            el("time", { class: "rel-time err-age", datetime: r.ts || "", "data-ts": r.ts || "", text: relativeTime(r.ts) }),
            el("span", { class: "err-claim", text: r.acked ? ("claimed by " + ((r.acked || {}).by || "someone")) : "unclaimed" })
          ].filter(Boolean)),
          el("span", { class: "err-msg", text: r.message || "" }),
          el("span", { class: "err-meta mono", text: (r.source || "") + " · " + (r.fingerprint || "")
            + (r.occurrences_since_last ? " · \u00d7" + (1 + r.occurrences_since_last) : "") })
        ]);
        item.addEventListener("click", function () { openErrorDetail(r); });
        body.appendChild(item);
      });
      body.appendChild(el("p", { class: "cell-sub", style: "margin-top:8px", text:
        "Claim a signature with POST api/ack-error {fingerprint} — acking collapses it off the queue without deleting the rows." }));
    }
    var deferred = rows.length - onBar.length;
    if (deferred > 0) {
      body.appendChild(el("p", { class: "cell-sub", text:
        deferred + " row" + (deferred === 1 ? "" : "s") + " below the bar (foreign clients, warnings, recovered blips) — never dropped; errors.json?include=deferred shows them." }));
    }
    var srcs = (meta.top_sources || []).slice(0, 3).map(function (s) { return s.source + " \u00d7" + s.count; }).join("  ·  ");
    if (srcs) body.appendChild(el("p", { class: "cell-sub", text: "top sources: " + srcs }));
    // COVERAGE: is this everything? Each channel that CAN report, and whether it has — the
    // one question an empty list can never answer about itself, rendered instead of implied.
    var cov = meta.coverage || {};
    if ((cov.channels || []).length) {
      var covWrap = el("div", { class: "err-coverage" });
      covWrap.appendChild(el("span", { class: "err-cov-lbl", text: "channels" }));
      cov.channels.forEach(function (c) {
        covWrap.appendChild(el("span", {
          class: "err-cov-chip" + (c.silent ? " is-silent" : " is-live"),
          title: c.wired || c.key,
          text: c.label + (c.silent ? " — silent" : " · " + c.rows)
        }));
      });
      body.appendChild(covWrap);
    }
    return el("section", { class: "card errors-card", id: "errorsCard", "aria-labelledby": "errorsTitle" }, [
      el("div", { class: "card-header" }, [
        el("div", { class: "card-title", id: "errorsTitle" }, [icon("warning"),
          doc.createTextNode("Operational errors" + (meta.unclaimed ? "  ·  " + meta.unclaimed + " unclaimed" : ""))]),
        el("span", { class: "badge b-" + (meta.unclaimed ? "fail" : "pass"),
                     text: meta.unclaimed ? String(meta.unclaimed) : "clear" })
      ]),
      body
    ]);
  }

  /* ---- UPDATES: the agents' own first-person feed of what they DID ----
     Chat-style, newest first, streamed over the same live tick as everything else. Evidence
     that is a URL is a link; anything else (a sha, a path) is shown as code, never invented. */
  var UPDATE_GLYPH = { fixed: "✓", shipped: "↑", answered: "↩", acked: "✓",
                       escalated: "⤴", noop: "·" };
  function updatesCard(rows) {
    rows = rows || [];
    var day = Date.now() / 1000 - 86400;
    var fresh = rows.filter(function (u) { return (u.epoch || 0) >= day; }).length;
    var body = el("div", { class: "card-body" });
    if (!rows.length) {
      body.appendChild(el("p", { class: "cell-sub", text:
        "Nothing posted yet. An agent narrates a fix with the client (update --note … --evidence …); unattended agents post on answer, ack and finish by themselves." }));
    }
    var feed = el("ol", { class: "updates-feed", "aria-label": "agent updates, newest first" });
    rows.slice(0, 12).forEach(function (u) {
      var ev = String(u.evidence || "");
      var evNode = !ev ? null : /^https?:\/\//i.test(ev)
        ? el("a", { class: "update-ev", href: ev, target: "_blank", rel: "noopener noreferrer", text: ev })
        : el("code", { class: "update-ev", text: ev });
      feed.appendChild(el("li", { class: "update-bubble", "data-kind": u.kind || "fixed",
                                  "data-focus-key": "upd:" + (u.epoch || "") + ":" + (u.agent || "") }, [
        el("span", { class: "update-glyph", "aria-hidden": "true", text: UPDATE_GLYPH[u.kind] || "•" }),
        el("div", { class: "update-body" }, [
          el("div", { class: "update-meta" }, [
            el("strong", { text: u.agent || "an agent" }),
            u.machine ? el("span", { text: " · " + u.machine }) : null,
            el("span", { text: " · " + (u.kind || "fixed") + (u.by ? " (" + u.by + ")" : "") + " · " }),
            el("time", { class: "rel-time", datetime: u.at || "", "data-ts": u.at || "", text: relativeTime(u.at) })
          ].filter(Boolean)),
          el("p", { class: "update-summary", text: u.summary || "" }),
          evNode
        ].filter(Boolean))
      ]));
    });
    if (rows.length) body.appendChild(feed);
    return el("section", { class: "card updates-card", id: "updatesCard", "aria-labelledby": "updatesTitle" }, [
      el("div", { class: "card-header" }, [
        el("div", { class: "card-title", id: "updatesTitle" }, [icon("pulse"),
          doc.createTextNode("Agent updates")]),
        el("span", { class: "badge b-" + (fresh ? "pass" : "stale"), text: fresh + " in 24h" })
      ]),
      body
    ]);
  }

  /* ---- WORK HEALTH: is "in progress" true? (hub_core.task_health) ----
     Every in-progress task in exactly one bucket, each with the reason the hub gave. A task
     whose checkpoints are all recorded reads "ready to close" only when something names a
     commit/URL/path to check AND no checkpoint says work remains — never "done". */
  function taskButton(id, title, sub, tone) {
    var rec = BY_ID[id];
    var node = el("button", { class: "attn-item t-" + (tone || "info"), type: "button",
      "data-focus-key": "health:" + id, "aria-label": (title || id) + (sub ? " — " + sub : "") }, [
      el("span", { class: "attn-kind b-" + (tone || "info"), text: localId(id) }),
      el("span", { class: "attn-body" }, [
        el("span", { class: "attn-title", text: title || id }),
        sub ? el("span", { class: "attn-reason", text: sub }) : null
      ].filter(Boolean))
    ]);
    if (rec) node.addEventListener("click", function () { openEntity("task", rec); });
    else { node.disabled = true; node.title = "not on this board snapshot"; }
    return node;
  }
  function openDecisions() {
    return (D.tasks || []).filter(function (t) {
      return t && t.work_kind === "decision" && t.status !== "done" && t.status !== "dropped" && !t.decision;
    });
  }
  function workstreamCard(th) {
    th = th || {};
    var counts = th.counts || {};
    var body = el("div", { class: "attn-list" });
    function group(label, rows, tone, sub) {
      if (!rows || !rows.length) return;
      body.appendChild(el("div", { class: "cell-sub", style: "margin:8px 0 4px", text: label + " · " + rows.length }));
      rows.slice(0, 6).forEach(function (r) { body.appendChild(taskButton(r.id, r.title, sub(r), tone)); });
    }
    group("Ready to close", th.complete_unclosed, "warn", function (r) {
      return r.says_unfinished ? ("a checkpoint says work remains: " + r.says_unfinished)
           : (r.closeable ? "all " + r.total + " checkpoints recorded · has a commit/URL/path to verify"
                          : "all " + r.total + " checkpoints recorded · nothing to verify against");
    });
    group("Stalled", th.stalled, "fail", function (r) { return r.reason; });
    group("Orphaned", th.orphaned, "fail", function (r) { return r.reason; });
    group("Unattended requests nobody started", th.unstarted, "warn", function (r) { return r.body; });
    var decisions = openDecisions();
    if (decisions.length) {
      body.appendChild(el("div", { class: "cell-sub", style: "margin:8px 0 4px", text: "Decisions waiting on a person · " + decisions.length }));
      decisions.slice(0, 6).forEach(function (t) { body.appendChild(taskButton(t.id, t.title, "a person's call — open it to decide", "info")); });
    }
    if (!body.children.length) {
      body.appendChild(el("div", { class: "attn-clear" }, [
        el("span", { class: "b-glyph", "aria-hidden": "true", text: GLYPH.pass }),
        doc.createTextNode(counts.in_progress ? " Every in-progress task moved within the last 30 minutes."
                                              : " Nothing is in progress.")]));
    }
    var moving = counts.moving || 0, total = counts.in_progress || 0;
    return el("section", { class: "card attention-card", id: "workstreamCard", "aria-labelledby": "wsTitle" }, [
      el("div", { class: "card-header" }, [
        el("div", { class: "card-title", id: "wsTitle" }, [icon("checks"),
          doc.createTextNode("Workstream  ·  " + moving + " of " + total + " in progress actually moving")])
      ]),
      el("div", { class: "card-body" }, [body])
    ]);
  }

  /* ---- NEEDS ATTENTION: operational conditions with who acts, the fix, values and age ---- */
  function needsAttentionCard(na) {
    na = na || {};
    var items = na.items || [];
    var body = el("div", { class: "attn-list" });
    var TONE = { critical: "fail", warn: "warn", info: "info" };
    items.slice(0, 10).forEach(function (it) {
      var tone = TONE[it.severity] || "info";
      var node = el("button", { class: "attn-item t-" + tone, type: "button", "data-focus-key": "na:" + it.id,
        "aria-label": it.severity + ": " + it.title }, [
        el("span", { class: "attn-kind b-" + tone, text: it.severity }),
        el("span", { class: "attn-body" }, [
          el("span", { class: "attn-title", text: it.title }),
          el("span", { class: "attn-reason", text: "who: " + (it.who || "—") + "  ·  standing " + fmtAge(it.age_s || 0) })
        ])
      ]);
      node.addEventListener("click", function () { openAttentionItem(it); });
      body.appendChild(node);
    });
    if (!items.length) body.appendChild(el("div", { class: "attn-clear" }, [
      el("span", { class: "b-glyph", "aria-hidden": "true", text: GLYPH.pass }),
      doc.createTextNode(" " + (na.verdict || "nothing needs attention"))]));
    var cleared = (na.recently_cleared || []).length;
    var failing = Object.keys(na.sources || {}).filter(function (k) { return na.sources[k] !== "ok"; });
    return el("section", { class: "card attention-card", id: "needsAttentionCard", "aria-labelledby": "naTitle" }, [
      el("div", { class: "card-header" }, [
        el("div", { class: "card-title", id: "naTitle" }, [icon("warning"),
          doc.createTextNode("Needs attention" + (items.length ? "  ·  " + items.length : ""))]),
        el("span", { class: "cell-sub", text: (cleared ? cleared + " recently cleared" : "") +
          (failing.length ? (cleared ? " · " : "") + "not checked: " + failing.join(", ") : "") })
      ]),
      el("div", { class: "card-body" }, [
        items.length ? el("p", { class: "cell-sub", style: "margin-bottom:8px", text: na.verdict || "" }) : null,
        body
      ].filter(Boolean))
    ]);
  }
  function openAttentionItem(it) {
    var body = el("div");
    body.appendChild(el("div", { class: "detail-grid one" }, [section("Condition", "warning", [
      row("Severity", it.severity), row("Who acts", it.who || "—"),
      it.detail ? row("Detail", it.detail) : null,
      rowMono("Fix", it.fix), rowMono("Standing", fmtAge(it.age_s || 0)),
      rowMono("Values", JSON.stringify(it.evidence || {}))
    ])]));
    var subject = (it.evidence || {}).task;
    if (subject && BY_ID[subject]) body.appendChild(el("div", { class: "detail-grid one" }, [section("Subject", "checks", [row("Task", chipRow([subject], "task"))])]));
    openModal(it.severity === "critical" ? "fail" : "warn", it.title, it.kind, "warning", body);
  }

  /* ---- CONSOLES: attended vs unattended, honest idle, crossovers ---- */
  function consoleLine(sess) {
    return el("li", { class: "agent-sess" }, [
      el("span", { class: "sess-id", text: sess.session || "?" }),
      el("span", { class: "sess-held", text: sess.agent + (sess.machine ? "@" + sess.machine : "") }),
      sess.task_id ? el("span", { class: "sess-held", text: "holds " + localId(sess.task_id) }) : null,
      el("span", { class: "sess-focus", text: sess.activity || sess.focus || sess.state || "" })
    ].filter(Boolean));
  }
  function openRunDetail(sess) {
    var rows = [
      row("State", sess.state + (sess.outcome ? " · " + sess.outcome : "")),
      row("Doing", sess.activity || "—"),
      sess.subject ? row("Subject", BY_ID[sess.subject] ? chipRow([sess.subject], "task") : sess.subject) : null,
      sess.subject_title ? row("Subject title", sess.subject_title) : null,
      sess.phase ? rowMono("Phase", sess.phase) : null,
      sess.narration ? row("Last narration", sess.narration) : null,
      sess.last_result ? rowMono("Latest result", sess.last_result) : null,
      (sess.targets || []).length ? rowMono("Targets", sess.targets.join(", ")) : null,
      sess.started ? rowMono("Started", fmtAge(Date.now() / 1000 - sess.started) + " ago") : null,
      sess.bounded_s ? rowMono("Bounded to", fmtAge(sess.bounded_s)) : null,
      sess.ended ? rowMono("Ended", fmtAge(Date.now() / 1000 - sess.ended) + " ago") : null,
      rowMono("Console", (sess.session || "?") + " · " + sess.agent + (sess.machine ? "@" + sess.machine : "")),
      sess.run ? rowMono("Run", sess.run) : null
    ];
    var body = el("div", null, [el("div", { class: "detail-grid one" }, [section("Unattended run", "pulse", rows)])]);
    openModal(sess.finished ? "info" : "warn", sess.subject_title || sess.subject || ("run " + (sess.run || sess.session)),
              sess.kind || "unattended", "pulse", body);
  }
  function consolesCard(sessions, crossovers) {
    sessions = sessions || {};
    var counts = sessions.counts || {};
    var body = el("div", { class: "card-body" });
    var attended = sessions.attended || [], un = sessions.unattended || [], fin = sessions.finished || [];
    body.appendChild(el("div", { class: "cell-sub", text: "Attended consoles · " + (counts.attended || 0) }));
    if (attended.length) body.appendChild(el("ul", { class: "agent-sessions" }, attended.slice(0, 8).map(consoleLine)));
    var totalUn = (counts.unattended || 0) + (counts.finished || 0);
    body.appendChild(el("div", { class: "cell-sub", style: "margin-top:10px",
      text: (counts.unattended || 0) + " of " + totalUn + " unattended sessions running" +
            (fin.length ? " · " + fin.length + " finished in the last 30 min" : "") }));
    if (un.length) {
      var list = el("div", { class: "attn-list" });
      un.slice(0, 8).forEach(function (sess) {
        var b = el("button", { class: "attn-item t-" + (sess.state === "working" ? "info" : "warn"), type: "button",
          "data-focus-key": "run:" + sess.session }, [
          el("span", { class: "attn-kind b-" + (sess.state === "working" ? "info" : "warn"), text: sess.state }),
          el("span", { class: "attn-body" }, [
            el("span", { class: "attn-title", text: sess.subject_title || sess.subject || (sess.agent + " · " + sess.session) }),
            el("span", { class: "attn-reason", text: sess.activity || "" })
          ])
        ]);
        b.addEventListener("click", function () { openRunDetail(sess); });
        list.appendChild(b);
      });
      body.appendChild(list);
    }
    if (fin.length) {
      var recap = el("ul", { class: "agent-sessions" });
      fin.slice(0, 5).forEach(function (sess) {
        var li = consoleLine(sess);
        li.addEventListener("click", function () { openRunDetail(sess); });
        recap.appendChild(li);
      });
      body.appendChild(recap);
    }
    crossovers = crossovers || [];
    body.appendChild(el("div", { class: "cell-sub", style: "margin-top:10px", text: "Crossovers · " + crossovers.length }));
    crossovers.slice(0, 6).forEach(function (x) {
      body.appendChild(el("div", { class: "callout " + (x.kind === "file" || x.kind === "task" ? "warn" : "info") }, [
        el("span", { class: "b-glyph", "aria-hidden": "true", text: GLYPH[x.kind === "file" || x.kind === "task" ? "warn" : "pass"] }),
        el("div", null, [el("strong", { text: x.kind + " " }), doc.createTextNode(x.detail + " — " +
          (x.a.agent || "?") + " (" + (x.a.session || "?") + ") and " + (x.b.agent || "?") + " (" + (x.b.session || "?") + ")")])
      ]));
    });
    return el("section", { class: "card", id: "consolesCard", "aria-labelledby": "consolesTitle" }, [
      el("div", { class: "card-header" }, [
        el("div", { class: "card-title", id: "consolesTitle" }, [icon("users"), doc.createTextNode("Consoles")])
      ]),
      body
    ]);
  }

  function overviewHeading(kicker, title, copy) {
    return el("div", { class: "overview-heading" }, [
      el("span", { class: "overview-heading-kicker", text: kicker }),
      el("div", { class: "overview-heading-copy" }, [
        el("h2", { text: title }),
        el("p", { text: copy })
      ])
    ]);
  }

  function buildOverview() {
    var pane = el("div", { class: "tab-content", id: "tab-overview", role: "tabpanel",
      "aria-labelledby": "tab-btn-overview", tabindex: "0" });
    var scroll = el("div", { class: "overview-scroll" });
    var au = D.audit || {}, b = D.build || {}, L = live();
    var activity = L.activity || [];

    scroll.appendChild(progressHero(L.progress, L.readiness, L.fleet, L.telemetry, L.cost, L.wip, L.attention));
    scroll.appendChild(overviewHeading("Now", "Execution and intervention",
      "See who is advancing work and the decisions that can change throughput immediately."));
    scroll.appendChild(el("div", { class: "operations-grid" }, [
      fleetView(L.fleet, L.worker_health), attentionRail(L.attention)
    ]));

    // The ask/answer loop and the operational stream, side by side: who is blocked on a
    // fact, and what is broken — the two queues that must never sit unread.
    scroll.appendChild(overviewHeading("Signals", "Questions and failures",
      "A blocked person and an unclaimed failure are the two most expensive things a board can let sit."));
    scroll.appendChild(el("div", { class: "operations-grid" }, [
      asksCard(), errorsCard(L.errors, L.error_log)
    ]));

    // Is "in progress" true, and what needs a person? The workstream buckets and the
    // operational attention list (owner, fix, values, age), then every live console —
    // attended, unattended runs, finished recaps — with the crossovers between them.
    scroll.appendChild(overviewHeading("Health", "Work that is really moving",
      "In-progress tasks nobody is moving, decisions waiting on a person, and conditions only a person can fix."));
    scroll.appendChild(el("div", { class: "operations-grid" }, [
      workstreamCard(L.task_health), needsAttentionCard(L.needs_attention)
    ]));
    scroll.appendChild(el("div", { class: "operations-grid" }, [consolesCard(L.sessions, L.crossovers)]));

    var dc = dagCard(L.dag);
    scroll.appendChild(overviewHeading("Next", "The pullable frontier",
      "Ready work and dependency shape reveal the fastest truthful path forward."));
    scroll.appendChild(el("div", { class: "next-grid" }, [readinessRail(L.readiness), dc].filter(Boolean)));

    var delivery = deliveryCard(L.delivery);

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
    scroll.appendChild(overviewHeading("Outcome", "From completion to reality",
      "Follow delivered work through the branch, release, live state, and canonical event record."));
    scroll.appendChild(el("div", { class: "outcome-grid" }, [delivery, actCard].filter(Boolean)));
    scroll.appendChild(overviewHeading("Narrative", "In the agents' own words",
      "What each agent says it fixed, answered or shipped — with the evidence behind it."));
    scroll.appendChild(el("div", { class: "narrative-grid" }, [updatesCard(L.updates)]));

    // audit card
    var auBody = el("div", { class: "card-body" });
    auBody.appendChild(el("p", { class: "cell-sub", style: "margin-bottom:12px",
      text: "Structural snapshot · exit " + (au.exit_code) + " · critical " + ((au.counts || {}).critical || 0) + " · high " + ((au.counts || {}).high || 0) + " · warn " + ((au.counts || {}).warn || 0) }));
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
        el("div", { text: "No structural violations in this snapshot." })]));
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

    var fm = failureModes(L.failure_modes);

    function ci(label, val) { return el("span", { class: "ci" }, [doc.createTextNode(label + " "), el("code", { text: val == null ? "—" : String(val) })]); }
    var coCard = el("section", { class: "card" }, [
      el("div", { class: "card-header" }, [
        el("div", { class: "card-title" }, [icon("rocket"), doc.createTextNode("Build coherence")]),
        el("span", { class: "badge b-" + (b.coherent === true ? "pass" : (b.coherent === false ? "fail" : "stale")),
                     text: b.coherent === true ? "coherent" : (b.coherent === false ? "drift" : "unmeasured") })]),
      el("div", { class: "card-body" }, [el("div", { class: "coherence-strip" }, [
        ci("repo", b.repo), ci("deploy", b.deploy), ci("stamped sha", b.sha), ci("HEAD", b.head), ci("served", b.served_sha)
      ])])
    ]);

    var adherence = adherenceCard(L.adherence);
    var integrityCards = [adherence, phCard, auCard, fm, coCard].filter(Boolean);
    var integrity = el("details", { class: "integrity-drawer" }, [
      el("summary", { class: "integrity-summary" }, [
        el("span", { class: "integrity-summary-icon", "aria-hidden": "true" }, [icon("stack")]),
        el("span", { class: "integrity-summary-copy" }, [
          el("strong", { text: "Integrity and system depth" }),
          el("span", { text: "Adherence, phases, structural audit, failure modes, and build identity" })
        ]),
        el("span", { class: "badge b-" + (au.ok ? "pass" : "warn"), text: au.ok ? "structurally clear" : "attention" })
      ]),
      el("div", { class: "integrity-grid" }, integrityCards)
    ]);
    scroll.appendChild(integrity);

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

  var _openModalEntity = null;
  // The open dialog's subject when it is NOT a ledger entity (an operational error signature,
  // a fleet seat). Those rows live in the live block rather than BY_ID, so they need their own
  // handle to stay current — the dialog contract is about the reader, not the storage.
  var _openModalLive = null;
  function openEntity(type, r, liveRefresh) {
    var role = type === "deploy" ? (r.audit_ok ? "pass" : "fail") : roleOf(type, r.status || r.maturity);
    var iconName = { task: "checks", adr: "branch", feat: "package", gap: "warning", cap: "stack", deploy: "rocket", directive: "bolt" }[type] || "info";
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
    if (type === "directive") {
      // Delivery is the directive's whole point: name the roster and who has checked in.
      var ackedBy = (D.acks || []).filter(function (a) { return a.directive === r.id; })
                                  .map(function (a) { return a.agent; });
      detailRows.push(row("Targets", (r.targets || []).join(", ") || "—"));
      detailRows.push(row("Acked by", ackedBy.length ? ackedBy.join(", ")
        : ((r.targets || []).indexOf("all") >= 0 ? "open roster — 'all' has no checklist"
                                                 : "nobody yet")));
      if (r.deadline) detailRows.push(rowMono("Deadline", r.deadline));
      if (r.remediation_cmd) detailRows.push(rowMono("Remediation", r.remediation_cmd));
    }
    var grid = el("div", { class: "detail-grid" + (detailRows.filter(Boolean).length ? "" : " one") }, [section("Identity", "info", idRows)]);
    if (detailRows.filter(Boolean).length) grid.appendChild(section("Detail", iconName, detailRows));
    body.appendChild(grid);

    // LIVE: what is happening now, plus the optional transient probe when this task explicitly
    // declared a critical boundary. Ordinary done work stands on the completed operation itself.
    if (type === "task") {
      var lease = leaseOf(r.id), prog = taskProgress(r), proof = completionProof(r), rec = proof.receipt;
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
      if (proof.declared) liveRows.push(rowMono("Declared critical probe", proof.command));
      if (rec) {
        liveRows.push(row("Critical-probe receipt", el("div", { class: "receipt-box" + (rec.exit_code === 0 ? " ok" : " bad") }, [
          el("div", { class: "mono", text: rec.command || "" }),
          el("div", { class: "cell-sub", text: "exit " + rec.exit_code + (rec.ran_by ? (" · ran by " + rec.ran_by) : "") + (rec.ran_at ? (" · " + rec.ran_at) : "") }),
          rec.output_sha256 ? el("div", { class: "cell-sub mono", text: "output sha256 " + rec.output_sha256 }) : null
        ].filter(Boolean))));
      } else if (r.status === "done" && proof.declared) {
        liveRows.push(row("Critical-probe receipt", el("div", { class: "callout warn" }, [
          el("span", { class: "b-glyph", "aria-hidden": "true", text: GLYPH.warn }),
          el("div", { text: "This task declared a critical probe, but no matching receipt was recorded." })])));
      } else if (r.status === "done") {
        liveRows.push(row("Completion", el("div", { class: "receipt-box ok" }, [
          el("div", { text: "Real operation completed" }),
          el("div", { class: "cell-sub", text: "No separate critical probe was declared or required." })
        ])));
      }
      if (liveRows.length) body.appendChild(el("div", { class: "detail-grid one" }, [section("Live", "pulse", liveRows)]));
      var cps = checkpointRows(r);
      if (cps.length) body.appendChild(el("div", { class: "detail-grid one" }, [section("Checkpoints", "checks", cps)]));
      var lane = [
        r.unattended ? row("Queue", "unattended — offered to unattended workers (P0-P2)") : null,
        r.project ? rowMono("Project", r.project) : null,
        r.auto_close ? row("Automatic close", r.auto_close.state + (r.auto_close.why ? " — " + r.auto_close.why : "") +
                                               (r.auto_close.sha ? " (commit " + r.auto_close.sha + ")" : "")) : null
      ].filter(Boolean);
      if (lane.length) body.appendChild(el("div", { class: "detail-grid one" }, [section("Lane", "rocket", lane)]));
      if (r.work_kind === "decision") {
        var dec = r.decision;
        var decRows = dec ? [
          row("Decided", dec.text), rowMono("By", dec.decided_by + " · " + dec.decided_at),
          rowMono("Then", dec.then), dec.followup ? row("Build task", chipRow([dec.followup], "task")) : null
        ] : [
          row("Waiting on", "a person's call — never an unattended worker's"),
          rowMono("Decide", "python -m hub_core.client decide " + r.id + ' --then file --decision "..."'),
          rowMono("Or close", "python -m hub_core.client decide " + r.id + ' --then close --decision "..."'),
          rowMono("Or reply", "python -m hub_core.client decide " + r.id + ' --then reply --decision "<question>"'),
          row("Who", "a named decider's own credential (HUB_DECIDERS); agent tokens are refused")
        ];
        body.appendChild(el("div", { class: "detail-grid one" }, [section("Decision", "branch", decRows)]));
      }
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
    if (typeof r.answers === "string" && r.answers) links.push(row("Answers", chipRow([r.answers], "note")));
    if (typeof r.supersedes === "string" && r.supersedes) links.push(row("Supersedes", chipRow([r.supersedes], "directive")));
    if (r.verified_by && r.verified_by.length) links.push(row("Verified by", el("div", null, r.verified_by.map(function (s) { return el("div", { class: "detail-prose", text: "• " + s }); }))));
    if (links.length) body.appendChild(el("div", { class: "detail-grid one" }, [section("Links & evidence", "branch", links)]));

    ["body_md", "context_md", "decision_md", "consequences_md"].forEach(function (f) {
      if (r[f]) body.appendChild(el("div", { class: "detail-grid one" }, [section(f.replace("_md", "").replace(/^./, function (c) { return c.toUpperCase(); }), "info", [el("div", { class: "detail-prose", text: r[f] })])]));
    });

    if (r.provenance) {
      var pv = r.provenance;
      body.appendChild(el("div", { class: "detail-grid one" }, [section("Provenance", "info", [
        rowMono("Created", pv.created_at), rowMono("Updated", pv.updated_at), pv.agent ? rowMono("Agent", pv.agent) : null,
        pv.commits && pv.commits.length ? rowMono("Commits", pv.commits.join(", ")) : null
      ])]));
    }
    if (liveRefresh) {
      refreshModal(role, title, r.id, iconName, body);
    } else {
      _openModalEntity = { type: type, id: r.id };
      openModal(role, title, r.id, iconName, body);
      try { history.replaceState(null, "", "#" + type + "-" + localId(r.id)); } catch (e) {}
    }
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
  function refreshModal(role, title, subtitle, iconName, bodyNode) {
    var m = doc.getElementById("universalModal");
    if (!m || !m.classList.contains("show")) return;
    var b = doc.getElementById("modalBody");
    var top = b ? b.scrollTop : 0, left = b ? b.scrollLeft : 0;
    var focus = elementKey(doc.activeElement);
    var box = doc.getElementById("modalIcon");
    box.className = "modal-icon t-" + role; box.textContent = ""; box.appendChild(icon(iconName));
    doc.getElementById("modalTitle").textContent = title;
    doc.getElementById("modalSubtitle").textContent = subtitle || "";
    if (b) { b.textContent = ""; b.appendChild(bodyNode); b.scrollTop = top; b.scrollLeft = left; }
    var restored = findElement(focus);
    if (restored && restored !== doc.activeElement) {
      try { restored.focus({ preventScroll: true }); } catch (e) { restored.focus(); }
    }
  }
  function refreshOpenEntityModal() {
    if (!_openModalEntity) return;
    var m = doc.getElementById("universalModal");
    if (!m || !m.classList.contains("show")) return;
    var current = BY_ID[_openModalEntity.id];
    if (!current) {
      closeModal();
      announce("The open Hub entity is no longer available.");
      return;
    }
    openEntity(_openModalEntity.type, current, true);
  }
  function refreshOpenLiveModal() {
    if (!_openModalLive) return;
    var m = doc.getElementById("universalModal");
    if (!m || !m.classList.contains("show")) return;
    var L = live(), fresh = null;
    if (_openModalLive.kind === "agent") {
      fresh = (L.fleet || []).filter(function (c) { return c.agent === _openModalLive.key; })[0];
      if (fresh) { openAgentDetail(fresh, true); return; }
      // The seat left the fleet view (idle past the window, or forgotten). Say so rather than
      // leaving a confident stale answer on the coordination surface.
      closeModal();
      announce("That agent is no longer on the fleet view.");
      return;
    }
    fresh = (L.errors || []).filter(function (r) {
      return (r.fingerprint || r.source || "") === _openModalLive.key;
    })[0];
    if (fresh) { openErrorDetail(fresh, true); return; }
    closeModal();
    announce("That error is no longer in the retained window.");
  }
  function closeModal() {
    var m = doc.getElementById("universalModal");
    if (m) m.classList.remove("show");
    var shell = doc.getElementById("appShell");
    if (shell) { shell.inert = false; shell.removeAttribute("aria-hidden"); }
    doc.removeEventListener("keydown", _trapTab, true);
    if (_modalOpener && doc.contains(_modalOpener)) { try { _modalOpener.focus(); } catch (e) {} } // absorbs: element detached
    _modalOpener = null;
    _openModalEntity = null;
    _openModalLive = null;
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
    var proof = completionProof(task);
    var boardDrained = (((live().progress || {}).pct || 0) >= 100);
    var particles = null;
    if (boardDrained) {
      particles = el("span", { class: "completion-particles", "aria-hidden": "true" });
      for (var i = 0; i < 12; i++) particles.appendChild(el("i", { style: "--particle:" + i }));
    }
    // Celebrate real delivery by default. Only a task that declared a critical probe and lacks its
    // passing receipt gets the warning treatment; an absent, undeclared test is not a defect.
    var headline = proof.passed ? "Task complete · critical probe passed"
      : proof.declared ? "Task complete · critical probe needs attention"
      : "Task complete · work delivered";
    var celebration = el("div", { class: "completion-celebration" + (proof.complete ? "" : " not-proven") + (boardDrained ? " is-milestone" : ""), role: "status", "aria-live": "assertive" }, [
      particles,
      el("span", { class: "completion-check", "aria-hidden": "true", text: proof.complete ? "✓" : "◌" }),
      el("span", { class: "completion-copy" }, [
        el("strong", { text: headline }),
        el("span", { text: task.title || localId(task.id) })
      ])
    ].filter(Boolean));
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
    connected: false,
    failures: 0,
    dataHealthy: true,
    lastAppliedAt: Date.now(),
    snapshotJSON: JSON.stringify(D),
    syncing: false,
    reconcilePending: false,
    snapshotRequired: false,
    signaledCursor: ((live().cursor) || {}).seq || 0,
    patchQueue: []
  };
  function setStatus(state, text, meta) {
    var p = doc.getElementById("statusPill"); if (!p) return;
    var visual = state === "connected" ? "live" : state === "disconnected" ? "error" : state;
    p.className = "status-pill" + (visual ? " " + visual : "");
    p.setAttribute("data-state", state || "disconnected");
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
      + (!LIVE.dataHealthy ? " · recovery active" : "");
  }
  function renderConnectionStatus() {
    setStatus(LIVE.connected ? "connected" : "disconnected", LIVE.connected ? "Connected" : "Disconnected");
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
  function elementKey(node) {
    if (!node || node === doc.body || node === doc.documentElement) return null;
    if (node.id) return { kind: "id", value: node.id };
    var attrs = ["data-focus-key", "data-entity-id", "data-task-id", "data-agent", "data-seq"];
    for (var i = 0; i < attrs.length; i++) {
      if (node.getAttribute && node.getAttribute(attrs[i])) {
        return { kind: "attr", name: attrs[i], value: node.getAttribute(attrs[i]) };
      }
    }
    return null;
  }
  function findElement(key, root) {
    if (!key) return null;
    root = root || doc;
    if (key.kind === "id") {
      var byIdentity = doc.getElementById(key.value);
      return byIdentity && (root === doc || root.contains(byIdentity)) ? byIdentity : null;
    }
    var nodes = root.querySelectorAll("[" + key.name + "]");
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].getAttribute(key.name) === key.value) return nodes[i];
    }
    return null;
  }
  function captureViewState() {
    var panes = {};
    TABS.forEach(function (tab) {
      var pane = doc.getElementById("tab-" + tab.key);
      if (!pane) return;
      var table = pane.querySelector(".table-wrapper");
      panes[tab.key] = {
        top: pane.scrollTop, left: pane.scrollLeft,
        tableTop: table ? table.scrollTop : 0, tableLeft: table ? table.scrollLeft : 0
      };
    });
    return {
      pageX: global.scrollX || 0, pageY: global.scrollY || 0,
      focus: elementKey(doc.activeElement), opener: elementKey(_modalOpener), panes: panes
    };
  }
  function restoreViewState(state) {
    if (!state) return;
    TABS.forEach(function (tab) {
      var saved = state.panes[tab.key], pane = doc.getElementById("tab-" + tab.key);
      if (!saved || !pane) return;
      pane.scrollTop = saved.top; pane.scrollLeft = saved.left;
      var table = pane.querySelector(".table-wrapper");
      if (table) { table.scrollTop = saved.tableTop; table.scrollLeft = saved.tableLeft; }
    });
    var restored = findElement(state.focus);
    if (restored && restored !== doc.activeElement) {
      try { restored.focus({ preventScroll: true }); } catch (e) { restored.focus(); }
    }
    if (state.opener) _modalOpener = findElement(state.opener) || _modalOpener;
    try { global.scrollTo(state.pageX, state.pageY); } catch (e) {}
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
        var proof = changes[id] === "done" ? completionProof(current) : null;
        var completionCopy = proof && proof.passed ? "Completed · critical probe passed: "
          : proof && proof.declared ? "Completed · critical probe needs attention: "
          : "Completed: ";
        toast((changes[id] === "done" ? completionCopy
              : changes[id] === "blocked" ? "Blocked: " : "Started: ") + (current.title || localId(id)),
          changes[id] === "done" ? (proof.complete ? "success" : "error") : (changes[id] === "blocked" ? "error" : "info"));
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
    var viewState = captureViewState();
    var changes = taskDelta(D.tasks || [], next.tasks || []);
    var oldActivity = ((live().activity || [])[0] || {}).seq || 0;
    var prevProg = live().progress || {};
    var prevFleet = fleetSeqMap(live().fleet);
    D = next;
    rebuildIndex();
    rerenderTabs(changes);
    refreshOverview();
    refreshOpenEntityModal();
    refreshOpenLiveModal();
    reactCockpit(prevProg, prevFleet);
    publishClientState();
    restoreViewState(viewState);
    reactToChanges(changes);

    var cursor = live().cursor || {};
    if (typeof cursor.seq === "number") LIVE.cursor = cursor.seq;
    LIVE.lastEventAt = cursor.ts || LIVE.lastEventAt;
    var n = Object.keys(changes).length;
    if (n) announce(n + (n === 1 ? " task changed." : " tasks changed."));
    else if (((live().activity || [])[0] || {}).seq > oldActivity) announce("New canonical Hub activity received.");
    LIVE.dataHealthy = true;
    LIVE.lastAppliedAt = Date.now();
    resolveWorkerWatches();
    renderConnectionStatus();
    tickLiveMeta();
  }

  var TYPE_COLLECTION = { task: "tasks", adr: "adrs", feat: "feats", gap: "gaps", cap: "caps",
                          deploy: "deploys", note: "notes", directive: "directives", ack: "acks" };
  function applyDelta(payload) {
    // DELTA CONSUME: patch only the changed entities into in-memory state and re-render. The wire
    // carries the changed rows, never the whole board. The cockpit blocks ride along in
    // payload.live so the hero, fleet and rail move with the entities instead of lagging a full
    // sync behind — the most-watched part of the page must not be the last to update.
    var viewState = captureViewState();
    var changed = payload.changed || [];
    var removed = (payload.removed || []).map(function (item) { return typeof item === "string" ? item : item && item.id; }).filter(Boolean);
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
    if (removed.length) {
      Object.keys(TYPE_COLLECTION).forEach(function (type) {
        var key = TYPE_COLLECTION[type];
        D[key] = (D[key] || []).filter(function (ent) { return removed.indexOf(ent.id) < 0; });
      });
    }
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
    refreshOpenEntityModal();
    refreshOpenLiveModal();
    reactCockpit(prevProg, prevFleet);
    reactToChanges(changes);
    publishClientState();
    restoreViewState(viewState);
    var cursor = payload.cursor || {};
    LIVE.cursor = Math.max(LIVE.cursor, cursor.seq || 0);
    var liveCursor = ((payload.live || {}).cursor) || {};
    LIVE.lastEventAt = liveCursor.ts || cursor.ts || LIVE.lastEventAt;
    LIVE.dataHealthy = true;
    LIVE.lastAppliedAt = Date.now();
    resolveWorkerWatches();
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

  function servedSuffix() {
    var observed = D.build && D.build.served_sha;
    return observed ? "&served=" + encodeURIComponent(observed) : "";
  }

  function readDelta(since) {
    return timedFetch("delta.json?since=" + encodeURIComponent(since) + servedSuffix(), {
      credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" }
    }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    }).then(function (payload) {
      var seq = payload && payload.cursor ? payload.cursor.seq : null;
      if (typeof seq !== "number" || seq < since) throw new Error("cursor gap");
      applyDelta(payload);
    });
  }
  function recoverSnapshot(reason) {
    return timedFetch("?format=json" + servedSuffix(), {
      credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" }
    }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    }).then(function (snapshot) {
      applySnapshot(snapshot, reason || "recovery");
      LIVE.signaledCursor = Math.max(LIVE.signaledCursor, LIVE.cursor);
    });
  }
  function noteEventSignal(signal) {
    signal = signal || {};
    var seq = Number(signal.seq);
    if (isFinite(seq)) LIVE.signaledCursor = Math.max(LIVE.signaledCursor, seq);
    if (signal.ts) LIVE.lastEventAt = signal.ts;
  }
  function applyStreamPatch(payload) {
    if (!payload || !payload.cursor || typeof payload.cursor.seq !== "number" || !Array.isArray(payload.changed)) {
      reconnectForRecovery("A malformed live patch was rejected; reconnecting from the last canonical cursor.");
      return false;
    }
    noteEventSignal(payload.cursor);
    if (LIVE.syncing) { LIVE.patchQueue.push(payload); return true; }
    if (payload.cursor.seq < LIVE.cursor) {
      reconnectForRecovery("The live cursor regressed; reconnecting for a canonical snapshot.");
      return false;
    }
    applyDelta(payload);
    return true;
  }
  function drainPatchQueue() {
    var queue = LIVE.patchQueue.splice(0);
    queue.forEach(function (payload) {
      if (payload.cursor && payload.cursor.seq >= LIVE.cursor) applyDelta(payload);
    });
  }
  function reconcileCanonical(reason) {
    if (LIVE.syncing) return Promise.resolve();
    if (!LIVE.snapshotRequired && !LIVE.reconcilePending && LIVE.signaledCursor <= LIVE.cursor) return Promise.resolve();
    var snapshotFirst = LIVE.snapshotRequired;
    LIVE.snapshotRequired = false;
    LIVE.reconcilePending = false;
    LIVE.syncing = true;
    var work = snapshotFirst ? recoverSnapshot(reason) : readDelta(LIVE.cursor).catch(function () {
      return recoverSnapshot((reason || "event") + "-recovery");
    });
    var succeeded = false;
    return work.then(function () {
      succeeded = true;
      LIVE.failures = 0;
      LIVE.dataHealthy = true;
    }, function () {
      LIVE.failures += 1;
      LIVE.dataHealthy = false;
      LIVE.reconcilePending = false;
      LIVE.snapshotRequired = false;
      LIVE.signaledCursor = LIVE.cursor;
      announce("The live event stream is connected, but canonical recovery could not complete yet.");
    }).then(function () {
      LIVE.syncing = false;
      drainPatchQueue();
      renderConnectionStatus();
      tickLiveMeta();
      if (succeeded && (LIVE.snapshotRequired || LIVE.reconcilePending)) {
        reconcileCanonical("coalesced");
      }
    });
  }
  function requestRecovery(signal, reason, force, snapshotRequired) {
    signal = signal || {};
    var seq = Number(signal.seq);
    if (snapshotRequired) {
      LIVE.snapshotRequired = true;
      if (isFinite(seq)) LIVE.signaledCursor = seq;
    } else if (isFinite(seq)) {
      LIVE.signaledCursor = Math.max(LIVE.signaledCursor, seq);
    }
    if (signal.ts) LIVE.lastEventAt = signal.ts;
    LIVE.reconcilePending = LIVE.reconcilePending || !!force || LIVE.signaledCursor > LIVE.cursor;
    if (LIVE.reconcilePending && !LIVE.syncing) reconcileCanonical(reason || "recovery");
  }
  function reconnectForRecovery(message) {
    LIVE.dataHealthy = false;
    if (message) announce(message);
    disconnectLive();
    connectLive();
  }
  function disconnectLive() {
    var source = LIVE.source;
    LIVE.source = null;
    if (source) source.close();
    LIVE.connected = false;
    renderConnectionStatus();
  }
  function connectLive() {
    if (LIVE.source || !global.EventSource) { renderConnectionStatus(); return; }
    var source;
    try { source = new global.EventSource("live/events?since=" + encodeURIComponent(LIVE.cursor) + servedSuffix()); }
    catch (error) { renderConnectionStatus(); return; }
    LIVE.source = source;
    source.onopen = function () {
      if (source !== LIVE.source) return;
      LIVE.connected = true; LIVE.failures = 0;
      renderConnectionStatus(); tickLiveMeta();
    };
    source.addEventListener("ready", function (event) {
      if (source !== LIVE.source) return;
      try {
        var data = JSON.parse(event.data);
        LIVE.lastEventAt = data.ts || LIVE.lastEventAt;
        if (Number(data.seq) < LIVE.cursor) requestRecovery(data, "cursor-reset", true, true);
        else if (Number(data.seq) > LIVE.cursor || !LIVE.dataHealthy) requestRecovery(data, "cursor-catch-up", !LIVE.dataHealthy);
      } catch (error) {}
      tickLiveMeta();
    });
    source.addEventListener("heartbeat", function (event) {
      if (source !== LIVE.source) return;
      renderConnectionStatus();
      tickLiveMeta();
    });
    source.addEventListener("patch", function (event) {
      if (source !== LIVE.source) return;
      try {
        var payload = JSON.parse(event.data);
        if (applyStreamPatch(payload)) pulseBeacon();
      } catch (error) {
        reconnectForRecovery("A malformed live patch was rejected; reconnecting from the last canonical cursor.");
      }
    });
    source.addEventListener("hub", function (event) {
      if (source !== LIVE.source) return;
      var data = {};
      try { data = JSON.parse(event.data); } catch (error) {}
      noteEventSignal(data);
      // Identity-only envelopes are advisory compatibility signals. The canonical `patch` event
      // carries the state itself; HTTP reads remain confined to reconnect/gap recovery.
    });
    source.addEventListener("reconnect", function () {
      if (source !== LIVE.source) return;
      disconnectLive();
      connectLive();
    });
    source.onerror = function () {
      if (source !== LIVE.source) return;
      LIVE.connected = false;
      LIVE.failures += 1;
      renderConnectionStatus();
      // EventSource owns transport reconnection and preserves Last-Event-ID. Its next ready event
      // performs exactly one cursor catch-up when the canonical head is ahead.
    };
  }
  function pulseBeacon() {
    // A visible confirmation that the stream is carrying traffic RIGHT NOW, independent of
    // whether the payload happened to change anything this page is showing.
    flashClass(doc.getElementById("statusPill"), "beat", 700);
  }
  function startLive() {
    tickLiveMeta();
    renderConnectionStatus();
    connectLive();
    setInterval(tickRelativeTimes, 5000);
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
  var _workerWatches = [];
  function resolveWorkerWatches() {
    var now = (live().inflight || []).length;
    _workerWatches.slice().forEach(function (watch) {
      if (now <= watch.before) return;
      clearTimeout(watch.timer);
      _workerWatches.splice(_workerWatches.indexOf(watch), 1);
      toast(watch.count > 1 ? "Workers are on the board — leases claimed." : "Worker is on the board — lease claimed.", "success");
      flashClass(doc.getElementById("fleetCard"), "bumped", 1400);
    });
  }
  function watchForWorker(count) {
    // A launch is a HAND-OFF to a process outside the browser, and the board cannot see whether
    // it started. So it watches its own canonical signal — a new live lease appearing — and says
    // so either way rather than leaving the operator staring at an unchanged page.
    var before = (live().inflight || []).length;
    var watch = { before: before, count: count, timer: null };
    watch.timer = setTimeout(function () {
      var index = _workerWatches.indexOf(watch);
      if (index < 0) return;
      _workerWatches.splice(index, 1);
      toast("No lease appeared in 45s. Register the worker protocol handler, or check that the queue has ready work.", "error");
    }, 45000);
    _workerWatches.push(watch);
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
  // "Update Core Systems": the adopter's MANUAL repair pass (HUB_MAINTAIN_URL), in the navbar
  // where every other action lives. A repair action that exists but cannot be clicked is a
  // capability the board advertises and cannot deliver — and its real audience is exactly the
  // machine whose self-update loop is broken, which no amount of self-distribution can reach.
  // A plain link: nothing fetched, nothing intercepted; hidden when the adopter configured none.
  function initMaintain() {
    var cfg = D.maintain || {}, btn = doc.getElementById("maintainBtn");
    if (!btn || !cfg.url) return;
    btn.href = cfg.url;
    if (cfg.label) btn.textContent = cfg.label;
    btn.hidden = false;
  }
  function initLaunchControls() {
    initMaintain();
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
    var bm = doc.getElementById("brandMark");
    if (bm) {
      var authoredMark = doc.documentElement.getAttribute("data-mark") || "cube";
      bm.appendChild(icon(P[authoredMark] ? authoredMark : "cube"));
    }
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
    initLaunchControls();
    initDensityControl();
    var mo = doc.getElementById("universalModal");
    if (mo) mo.addEventListener("click", function (e) { if (e.target === mo) closeModal(); });
    var mc = doc.getElementById("modalClose"); if (mc) mc.addEventListener("click", closeModal);
    doc.addEventListener("keydown", function (e) { if (e.key === "Escape") closeModal(); });
    tickClock(); setInterval(tickClock, 1000);
    paintBackdrop();
    renderConnectionStatus();
    scheduleTaskAgeRefresh();

    global.HubCommands = [
      { id: "cmd:overview", title: "Go to Overview", sub: "tab", run: function () { activate("overview"); } },
      { id: "cmd:tasks", title: "Go to Tasks", sub: "tab", run: function () { activate("tasks"); } },
      { id: "cmd:fleet", title: "Show the fleet", sub: "verb", run: function () { focusCard("fleetCard"); } },
      { id: "cmd:attention", title: "What needs the operator?", sub: "verb", run: function () { focusCard("attentionCard"); } },
      { id: "cmd:asks", title: "Open questions", sub: "verb", run: function () { focusCard("asksCard"); } },
      { id: "cmd:errors", title: "Operational errors", sub: "verb", run: function () { focusCard("errorsCard"); } },
      { id: "cmd:directives", title: "Go to Directives", sub: "tab", run: function () { activate("directives"); } },
      { id: "cmd:adherence", title: "Board adherence", sub: "verb", run: function () { focusCard("adherenceCard"); } },
      { id: "cmd:dag", title: "Dependency frontier", sub: "verb", run: function () { focusCard("dagCard"); } },
      { id: "cmd:theme", title: "Toggle light / dark theme", sub: "verb", run: toggleTheme },
      { id: "cmd:density", title: "Cycle density", sub: "verb", run: cycleDensity },
      { id: "cmd:copy-link", title: "Copy deep link", sub: "verb", run: function () { try { navigator.clipboard.writeText(location.href); toast("Link copied", "success"); } catch (e) { toast("Copy failed", "error"); } } }
    ];

    var initial = "overview";
    try { var p = new URL(location.href).searchParams.get("tab"); if (p && _panes[p]) initial = p; } catch (e) {}
    var keyMap = { task: "tasks", adr: "adrs", feat: "feats", gap: "gaps", cap: "caps", deploy: "deploys", note: "notes", directive: "directives" };
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
  function setDensity(value, persist) {
    value = value === "compact" ? "compact" : "comfortable";
    doc.documentElement.setAttribute("data-density", value);
    var control = doc.getElementById("hub-density-select");
    if (control && control.value !== value) control.value = value;
    if (persist) try { localStorage.setItem("hub-density", value); } catch (e) {}
    return value;
  }
  function initDensityControl() {
    var control = doc.getElementById("hub-density-select");
    var current = doc.documentElement.getAttribute("data-density") || "comfortable";
    setDensity(current, false);
    if (control) control.addEventListener("change", function () {
      toast("Density: " + setDensity(control.value, true), "info");
    });
  }
  function cycleDensity() {
    var r = doc.documentElement, cur = r.getAttribute("data-density") || "comfortable";
    var next = cur === "compact" ? "comfortable" : "compact";
    setDensity(next, true); toast("Density: " + next, "info");
  }

  var _taskAgeTimer = null;
  function refreshTaskAges() {
    Array.prototype.forEach.call(doc.querySelectorAll("[data-task-age-sle]"), function (node) {
      var task = BY_ID[node.getAttribute("data-task-age-sle")];
      var next = task && taskAgeSleNode(task, node.closest && !!node.closest(".tcard"));
      if (!next) return;
      node.className = next.className;
      node.textContent = next.textContent;
      node.title = next.title;
    });
  }
  function scheduleTaskAgeRefresh() {
    if (_taskAgeTimer) clearTimeout(_taskAgeTimer);
    // Exact local clock derivation, not a server poll: wake on the next minute boundary so age and
    // SLE risk cannot wait for a board event to become truthful.
    var delay = 60020 - (Date.now() % 60000);
    _taskAgeTimer = setTimeout(function () { refreshTaskAges(); scheduleTaskAgeRefresh(); }, delay);
  }

  global.Hub = { toast: toast, setStatus: setStatus, activate: activate, openEntity: openEntity,
                 closeModal: closeModal, live: function () { return LIVE; } };

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", build);
  else build();
})(typeof window !== "undefined" ? window : this);
