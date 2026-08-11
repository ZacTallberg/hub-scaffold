/*
 * palette.js — hub shared frontend kit
 * ----------------------------------------------
 * A vanilla, zero-dependency Cmd/Ctrl-K COMMAND PALETTE + an always-visible inline
 * fuzzy FILTER, both indexing the canonical `<script id="hub-data">` JSON island.
 *
 * Contract (must match across the kit — tokens.css, scrollspy.js, every hub's hub.html):
 *   - Data island:   <script id="hub-data" type="application/json">{...}</script>
 *                    Reads via JSON.parse(document.getElementById('hub-data').textContent).
 *                    Payload shape: { state, tasks[], adrs[], feats[], gaps[], caps[], deploys[], audit, graph }.
 *   - Entity anchors: each row is <... id="<type>-<id>"> e.g. id="task-0147", id="adr-0017",
 *                     id="feat-tracking.eskf", id="cap-ar.world-lock", id="deploy-20260621ef".
 *                     The palette navigates to `#<type>-<localid>` (localid = the part after the last ':').
 *   - Status vocab:  FIXED 5-state pass|warn|fail|info|stale, rendered glyph + text + aria-label,
 *                    never color alone. Glyph map exported as HubPalette.GLYPH.
 *   - CSS hooks consumed (defined in tokens.css): --bg --surface --surface-2 --border --text
 *                    --muted --accent --ring --radius --nav-h, status role pairs --pass/--warn/
 *                    --fail/--info/--stale, and [data-density]. This file ships ONE injected
 *                    <style> for palette-only widget chrome, all expressed in those tokens.
 *
 * Public API (other kit files / hub.html may call these):
 *   - HubPalette.open()            open the Cmd-K palette
 *   - HubPalette.close()           close it
 *   - HubPalette.toggle()
 *   - HubPalette.data()            the parsed JSON island (cached)
 *   - HubPalette.index()           the flat searchable index (lazy-built on first open/filter)
 *   - HubPalette.GLYPH             { pass:'✓', warn:'▲', fail:'✕', info:'•', stale:'◌' }
 *   - HubPalette.fuzzy(q, hay)     -> {score:Number, hits:[indices]} | null   (subsequence scorer)
 *   - HubPalette.init()            idempotent; auto-runs on DOMContentLoaded
 *
 * Mount points (optional; created/located by init):
 *   - A trigger button [data-hub-palette-trigger] (any element) toggles the palette.
 *   - An inline filter: <input data-hub-filter> + a container of rows each marked
 *     [data-hub-row] carrying [data-hub-text] (the haystack) inside a scope element
 *     [data-hub-filter-scope]. A [data-hub-filter-count] element gets the aria-live tally.
 */
(function (global) {
  "use strict";

  var GLYPH = { pass: "✓", warn: "▲", fail: "✕", info: "•", stale: "◌" };
  // Doctrine entity groups, in palette display order. label = group heading; type = anchor prefix.
  var GROUPS = [
    { key: "commands", type: "cmd", label: "Commands" },
    { key: "tasks", type: "task", label: "Tasks" },
    { key: "adrs", type: "adr", label: "ADRs" },
    { key: "feats", type: "feat", label: "Features" },
    { key: "gaps", type: "gap", label: "Gaps" },
    { key: "caps", type: "cap", label: "Capabilities" },
    { key: "deploys", type: "deploy", label: "Deploys" }
  ];

  var _data = null, _index = null, _root = null, _input = null, _list = null,
      _status = null, _active = -1, _results = [], _prevFocus = null, _built = false;

  function $(sel, ctx) { return (ctx || document).querySelector(sel); }
  function $all(sel, ctx) { return Array.prototype.slice.call((ctx || document).querySelectorAll(sel)); }

  function data() {
    if (_data) return _data;
    var el = document.getElementById("hub-data");
    if (!el) { _data = {}; return _data; }
    try { _data = JSON.parse(el.textContent || "{}"); }
    catch (e) { _data = {}; }
    return _data;
  }

  // localid = the last colon-segment of an opaque id ("demo:task:0147" -> "0147");
  // bare ids pass through unchanged.
  function localId(id) {
    id = String(id == null ? "" : id);
    var i = id.lastIndexOf(":");
    return i >= 0 ? id.slice(i + 1) : id;
  }

  // Pull a usable status role (one of the fixed 5) off a record, falling back to "info".
  function statusOf(rec) {
    var s = (rec && (rec.status_role || rec.status)) || "";
    s = String(s).toLowerCase();
    if (GLYPH.hasOwnProperty(s)) return s;
    return null; // unknown/absent: caller shows no badge rather than a wrong one
  }

  // Build the flat, group-tagged search index ONCE (doctrine perf: lazy on first need).
  function buildIndex() {
    if (_index) return _index;
    var d = data(), out = [];
    GROUPS.forEach(function (g) {
      // "commands" are runnable verbs registered by the hub renderer (window.HubCommands),
      // not entities from the data island.
      var rows = g.key === "commands"
        ? (Array.isArray(global.HubCommands) ? global.HubCommands : [])
        : (Array.isArray(d[g.key]) ? d[g.key] : []);
      rows.forEach(function (rec) {
        if (!rec) return;
        var id = rec.id != null ? rec.id : (rec.key != null ? rec.key : "");
        var local = localId(id);
        var title = rec.title || rec.name || rec.summary || rec.label || rec.topic || String(local);
        var sub = rec.subtitle || rec.detail || rec.maturity || rec.build || rec.sha || "";
        // Commands may carry an explicit anchor/href + an action fn name; default to entity anchor.
        var anchor = rec.anchor || ("#" + g.type + "-" + local);
        var hay = [g.label, local, title, sub, rec.legacy_ref || "", (rec.tags || []).join(" ")]
          .join(" ").toLowerCase();
        out.push({
          group: g.label, type: g.type, id: id, local: local, title: String(title),
          sub: String(sub), anchor: anchor, status: statusOf(rec), hay: hay,
          run: typeof rec.run === "function" ? rec.run : null
        });
      });
    });
    _index = out;
    return out;
  }

  /*
   * fuzzy(query, haystack) — case-insensitive ordered SUBSEQUENCE scorer.
   * Returns null when not every query char is found in order; otherwise
   * { score, hits } where higher score = better. Rewards: consecutive runs,
   * word-boundary starts, and matches near the front. Used by both surfaces so
   * ranking is identical everywhere.
   */
  function fuzzy(query, haystack) {
    var q = String(query || "").toLowerCase();
    var h = String(haystack || "").toLowerCase();
    if (!q) return { score: 0, hits: [] };
    var hits = [], score = 0, qi = 0, prev = -2;
    for (var i = 0; i < h.length && qi < q.length; i++) {
      if (h[i] === q[qi]) {
        hits.push(i);
        var pts = 1;
        if (i === prev + 1) pts += 4;                       // consecutive run bonus
        if (i === 0 || /[\s\-_./:#]/.test(h[i - 1])) pts += 3; // word-boundary bonus
        pts += Math.max(0, 3 - i * 0.05);                   // earliness bonus (capped)
        score += pts;
        prev = i; qi++;
      }
    }
    if (qi < q.length) return null;                          // incomplete subsequence
    score -= (h.length - q.length) * 0.02;                  // mild length penalty
    return { score: score, hits: hits };
  }

  // HTML escape for safe rendering of arbitrary entity text.
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // Render `text` with <mark> around the matched subsequence positions in `hits`.
  function markup(text, hits) {
    if (!hits || !hits.length) return esc(text);
    var set = {}; hits.forEach(function (i) { set[i] = 1; });
    var out = "", open = false;
    for (var i = 0; i < text.length; i++) {
      var on = !!set[i];
      if (on && !open) { out += "<mark>"; open = true; }
      else if (!on && open) { out += "</mark>"; open = false; }
      out += esc(text[i]);
    }
    if (open) out += "</mark>";
    return out;
  }

  function statusBadge(role) {
    if (!role) return "";
    return '<span class="hp-status hp-status--' + role + '" aria-label="' + role + '">' +
      '<span class="hp-glyph" aria-hidden="true">' + GLYPH[role] + "</span>" +
      '<span class="hp-srt">' + role + "</span></span>";
  }

  // ---- Cmd-K palette ----
  function ensureDom() {
    if (_built) return;
    _built = true;
    injectStyle();
    _root = document.createElement("div");
    _root.className = "hp-overlay";
    _root.hidden = true;
    _root.id = "hub-palette";
    _root.innerHTML =
      '<div class="hp-panel" role="dialog" aria-modal="true" aria-label="Command palette">' +
        '<div class="hp-box" role="combobox" aria-haspopup="listbox" aria-expanded="true"' +
            ' aria-owns="hp-list" aria-controls="hp-list">' +
          '<span class="hp-kbd" aria-hidden="true">⌘K</span>' +
          '<input id="hp-input" class="hp-input" type="text" autocomplete="off"' +
            ' spellcheck="false" placeholder="Search tasks, ADRs, features, gaps, deploys…"' +
            ' role="searchbox" aria-autocomplete="list" aria-controls="hp-list"' +
            ' aria-label="Search the hub" aria-activedescendant="">' +
        "</div>" +
        '<ul id="hp-list" class="hp-list" role="listbox" aria-label="Results"></ul>' +
        '<div id="hp-status" class="hp-foot" role="status" aria-live="polite"></div>' +
      "</div>";
    document.body.appendChild(_root);
    _input = $("#hp-input", _root);
    _list = $("#hp-list", _root);
    _status = $("#hp-status", _root);

    _root.addEventListener("mousedown", function (e) { if (e.target === _root) close(); });
    _input.addEventListener("input", renderResults);
    _input.addEventListener("keydown", onKey);
    _list.addEventListener("mousedown", function (e) {
      var li = e.target.closest && e.target.closest("[role=option]");
      if (li) { e.preventDefault(); choose(parseInt(li.dataset.i, 10)); }
    });
    _list.addEventListener("mousemove", function (e) {
      var li = e.target.closest && e.target.closest("[role=option]");
      if (li) setActive(parseInt(li.dataset.i, 10), false);
    });
  }

  function renderResults() {
    var q = _input.value.trim();
    var idx = buildIndex(), scored = [];
    if (!q) {
      scored = idx.slice(0, 50).map(function (it) { return { it: it, hits: [], score: 0 }; });
    } else {
      for (var i = 0; i < idx.length; i++) {
        var r = fuzzy(q, idx[i].hay);
        if (r) {
          // re-run on the visible title so <mark> lands on what the eye sees
          var th = fuzzy(q, idx[i].title.toLowerCase());
          scored.push({ it: idx[i], hits: th ? th.hits : [], score: r.score + (th ? th.score : 0) });
        }
      }
      scored.sort(function (a, b) { return b.score - a.score; });
      scored = scored.slice(0, 50);
    }
    _results = scored;
    var html = "", lastGroup = null;
    scored.forEach(function (s, i) {
      if (s.it.group !== lastGroup) {
        lastGroup = s.it.group;
        html += '<li class="hp-group" role="presentation">' + esc(lastGroup) + "</li>";
      }
      html += '<li id="hp-opt-' + i + '" class="hp-opt" role="option" data-i="' + i +
        '" aria-selected="false">' +
        statusBadge(s.it.status) +
        '<span class="hp-opt-id">' + esc(s.it.local) + "</span>" +
        '<span class="hp-opt-title">' + markup(s.it.title, s.hits) + "</span>" +
        (s.it.sub ? '<span class="hp-opt-sub">' + esc(s.it.sub) + "</span>" : "") +
        "</li>";
    });
    _list.innerHTML = html || '<li class="hp-empty" role="presentation">No matches</li>';
    _status.textContent = scored.length + (scored.length === 1 ? " result" : " results");
    setActive(scored.length ? 0 : -1, true);
  }

  function setActive(i, scroll) {
    var opts = $all("[role=option]", _list);
    if (_active >= 0 && opts[_active]) opts[_active].setAttribute("aria-selected", "false");
    _active = i;
    if (i >= 0 && opts[i]) {
      opts[i].setAttribute("aria-selected", "true");
      _input.setAttribute("aria-activedescendant", opts[i].id);
      if (scroll !== false) opts[i].scrollIntoView({ block: "nearest" });
    } else {
      _input.setAttribute("aria-activedescendant", "");
    }
  }

  function move(delta) {
    if (!_results.length) return;
    var n = _results.length;
    var next = _active < 0 ? 0 : (_active + delta + n) % n;
    setActive(next, true);
  }

  function choose(i) {
    var s = _results[i];
    if (!s) return;
    close();
    // Runnable command verb (window.HubCommands): execute its action instead of navigating.
    if (s.it.run) { try { s.it.run(); } catch (e) {} return; }
    var anchor = s.it.anchor;
    var id = anchor.charAt(0) === "#" ? anchor.slice(1) : anchor;
    var target = document.getElementById(id);
    if (target) {
      // auto-expand a containing <details> so deep-links land open (doctrine §8)
      var det = target.closest && target.closest("details");
      if (det) det.open = true;
      if (target.tagName === "DETAILS") target.open = true;
      try { history.replaceState(null, "", anchor); } catch (e) {}
      target.scrollIntoView({ behavior: prefersReduced() ? "auto" : "smooth", block: "start" });
      flash(target);
      if (target.tabIndex < 0) target.setAttribute("tabindex", "-1");
      target.focus({ preventScroll: true });
    } else {
      location.hash = anchor;
    }
  }

  function flash(el) {
    if (prefersReduced()) return;
    el.classList.add("hp-flash");
    setTimeout(function () { el.classList.remove("hp-flash"); }, 1200);
  }

  function onKey(e) {
    switch (e.key) {
      case "ArrowDown": e.preventDefault(); move(1); break;
      case "ArrowUp": e.preventDefault(); move(-1); break;
      case "Home": e.preventDefault(); setActive(0, true); break;
      case "End": e.preventDefault(); setActive(_results.length - 1, true); break;
      case "Enter": e.preventDefault(); choose(_active); break;
      case "Escape": e.preventDefault(); close(); break;
      case "Tab": e.preventDefault(); break; // focus trap: only the input is tabbable
    }
  }

  function open() {
    ensureDom();
    if (!_root.hidden) return;
    _prevFocus = document.activeElement;
    _root.hidden = false;
    document.documentElement.classList.add("hp-open");
    _input.value = "";
    renderResults();
    _input.focus();
  }

  function close() {
    if (!_root || _root.hidden) return;
    _root.hidden = true;
    document.documentElement.classList.remove("hp-open");
    if (_prevFocus && _prevFocus.focus) { try { _prevFocus.focus(); } catch (e) {} }
  }

  function toggle() { (_root && !_root.hidden) ? close() : open(); }

  // ---- Always-visible inline fuzzy filter ----
  function wireInlineFilter() {
    _input2 = $("[data-hub-filter]");
    if (!_input2) return;
    var scope = $("[data-hub-filter-scope]") || document;
    var count = $("[data-hub-filter-count]");
    function run() {
      var q = _input2.value.trim();
      var rows = $all("[data-hub-row]", scope), shown = 0;
      rows.forEach(function (row) {
        var label = $("[data-hub-label]", row) || row;
        var raw = row.getAttribute("data-hub-text") || label.textContent || "";
        if (!q) {
          row.hidden = false; shown++;
          if (label.dataset.hubOrig != null) { label.innerHTML = label.dataset.hubOrig; delete label.dataset.hubOrig; }
          return;
        }
        var r = fuzzy(q, raw.toLowerCase());
        if (r) {
          row.hidden = false; shown++;
          var th = fuzzy(q, label.textContent.toLowerCase());
          if (th) {
            if (label.dataset.hubOrig == null) label.dataset.hubOrig = label.innerHTML;
            label.innerHTML = markup(label.textContent, th.hits);
          }
        } else {
          row.hidden = true;
        }
      });
      if (count) count.textContent = shown + (shown === 1 ? " match" : " matches");
    }
    _input2.addEventListener("input", run);
    run();
  }
  var _input2 = null;

  function prefersReduced() {
    return global.matchMedia && global.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function onGlobalKey(e) {
    if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault(); toggle();
    }
  }

  function init() {
    if (init._done) return; init._done = true;
    document.addEventListener("keydown", onGlobalKey);
    $all("[data-hub-palette-trigger]").forEach(function (el) {
      el.addEventListener("click", function (e) { e.preventDefault(); open(); });
    });
    wireInlineFilter();
  }

  function injectStyle() {
    if (document.getElementById("hp-style")) return;
    var css =
      ".hp-overlay{position:fixed;inset:0;z-index:1000;display:flex;justify-content:center;" +
        "align-items:flex-start;padding:max(8vh,2rem) 1rem 1rem;background:color-mix(in oklab,var(--bg) 60%,transparent);" +
        "backdrop-filter:blur(2px)}" +
      "html.hp-open{overflow:hidden}" +
      ".hp-panel{width:min(640px,100%);background:var(--surface);color:var(--text);" +
        "border:1px solid var(--border);border-radius:var(--radius);box-shadow:0 12px 40px rgba(0,0,0,.35);" +
        "overflow:hidden;display:flex;flex-direction:column;max-height:min(70vh,38rem)}" +
      ".hp-box{display:flex;align-items:center;gap:.5rem;padding:.5rem .75rem;border-bottom:1px solid var(--border)}" +
      ".hp-kbd{font-size:.75rem;color:var(--muted);border:1px solid var(--border);border-radius:calc(var(--radius)/2);" +
        "padding:.05rem .35rem;flex:none}" +
      ".hp-input{flex:1;min-width:0;background:transparent;border:0;color:var(--text);" +
        "font:inherit;font-size:1rem;padding:.4rem .15rem;min-height:24px}" +
      ".hp-input:focus{outline:none}" +
      ".hp-box:focus-within{outline:2px solid var(--ring);outline-offset:2px;border-radius:var(--radius)}" +
      ".hp-list{list-style:none;margin:0;padding:.25rem;overflow:auto;flex:1}" +
      ".hp-group{font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);" +
        "padding:.5rem .6rem .2rem;position:sticky;top:0;background:var(--surface)}" +
      ".hp-opt{display:flex;align-items:center;gap:.55rem;padding:.45rem .6rem;border-radius:calc(var(--radius)/1.5);" +
        "cursor:pointer;min-height:36px;line-height:1.25}" +
      ".hp-opt[aria-selected=true]{background:var(--surface-2);outline:2px solid var(--ring);outline-offset:-2px}" +
      ".hp-opt-id{font-variant-numeric:tabular-nums;color:var(--muted);font-size:.8rem;flex:none;min-width:3.5em}" +
      ".hp-opt-title{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}" +
      ".hp-opt-title mark,[data-hub-row] mark{background:transparent;color:var(--accent);font-weight:700;text-decoration:underline}" +
      ".hp-opt-sub{color:var(--muted);font-size:.8rem;flex:none;max-width:40%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}" +
      ".hp-empty,.hp-foot{padding:.7rem .8rem;color:var(--muted);font-size:.85rem}" +
      ".hp-foot{border-top:1px solid var(--border)}" +
      ".hp-status{display:inline-flex;align-items:center;gap:.25rem;flex:none}" +
      ".hp-glyph{display:inline-grid;place-items:center;width:1.2em;height:1.2em;border-radius:50%;" +
        "font-size:.75rem;line-height:1}" +
      ".hp-status--pass .hp-glyph{background:var(--pass);color:var(--pass-fg)}" +
      ".hp-status--warn .hp-glyph{background:var(--warn);color:var(--warn-fg)}" +
      ".hp-status--fail .hp-glyph{background:var(--fail);color:var(--fail-fg)}" +
      ".hp-status--info .hp-glyph{background:var(--info);color:var(--info-fg)}" +
      ".hp-status--stale .hp-glyph{background:var(--stale);color:var(--stale-fg)}" +
      ".hp-srt{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);clip-path:inset(50%);white-space:nowrap}" +
      ".hp-flash{animation:hp-flash 1.2s ease-out}" +
      "@keyframes hp-flash{0%{background:var(--accent);color:var(--bg)}100%{background:transparent}}" +
      "@media (prefers-reduced-motion: reduce){.hp-flash{animation:none;outline:2px solid var(--accent)}}" +
      "@media (max-width:640px){.hp-overlay{padding-top:2rem}.hp-opt-sub{display:none}}";
    var st = document.createElement("style");
    st.id = "hp-style";
    st.textContent = css;
    document.head.appendChild(st);
  }

  var HubPalette = {
    open: open, close: close, toggle: toggle, init: init,
    data: data, index: buildIndex, fuzzy: fuzzy, GLYPH: GLYPH
  };
  global.HubPalette = HubPalette;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})(typeof window !== "undefined" ? window : this);
