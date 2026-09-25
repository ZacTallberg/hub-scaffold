/* =============================================================================
   TABLE — table behaviours any app links.  v1.3.0

   A hosted component: an app LINKS this file from the hub and never copies it,
   so a change here reaches every adopting app's tables on their next page load,
   with no app deploy.

   WHAT IT DOES, each one only where the page has the markup for it:

   1. THE HEADING IS THE HANDLE. A <th data-field draggable="true"> moves its
      column when dragged; there is no grip glyph. Nothing in the BODY is ever
      draggable, because draggable kills text selection and the body is what
      people select to paste into a spreadsheet.
   2. PINS ARE GHOSTS UNTIL THEIR COLUMN IS UNDER THE POINTER. Every pinnable
      heading carries a pin that appears for the hovered heading, or the column a
      body cell is in. Pinned columns stick to the left edge as the table scrolls
      sideways; the pinned block's edge carries one rule, and a shadow once the
      table has scrolled.
   3. THE BAND NEVER WRAPS. A [data-ht-toolbar] stays one line: whatever does
      not fit moves into the "…" menu at the END of the band, least important
      first (the rank each item declares), and comes back in the same order as
      the window grows. Moved, not copied: one set of controls.
   4. SUGGEST (optional, only where the app declares a model lane). A person
      says what they are trying to do; the APP's endpoint asks its model and
      answers with lenses -- a titled cut of the table, a row count the app
      measured with its own filter, the URL that opens it. The component draws
      them. It never calls a model and never names one. A slow lane may answer
      202 and be polled.
   5. REQUEST IT (optional, only where the app declares a request lane). When
      the table cannot do what the person needs, the APP's endpoint files the
      person's words as work on the hub board; the component shows what it filed.
   6. EVERY COLUMN CAN BE SIZED. A handle on each heading's right edge: drag,
      double-click to fit the content, or arrow keys. A drag moves one thing (the
      table is frozen at its drawn widths first), and a column is never narrower
      than its own heading needs. The stylesheet draws TWO TIERS of faint line: a
      whisper between every column, the same line a step more present where the
      subject changes (.col-group / [data-ht-seam]).

   WHO OWNS WHAT. The component owns the INTERACTION; the app owns PERSISTENCE.
   A drop, a pin click, a resize and "open with these columns" are announced as
   cancelable events on the mount -- hub-table:reorder, :pin, :resize, :layout --
   and an app that saves layouts calls preventDefault() and saves them its own
   way. An app that does not gets the default: the change happens in the page and
   this viewer's order, pins and widths are remembered in localStorage.

   THE HUB SERVES THIS COMPONENT AND NEVER AN APP'S DATA. Every URL on the mount
   is a path on the APP's own origin, behind the app's own sign-in. This file
   holds no credential and calls the hub for nothing.

   THE MOUNT:

     <section data-hub-table data-app="budget-app"
              data-table="#ledger"                           (default: the first <table> inside)
              data-shape-url="/budget/ledger/shape"          optional
              data-request-url="/budget/ledger/request"      optional
              data-can-request="1"                           the person may file a request
              data-agent-powered                             a model is configured: draw the star
              data-shape-examples='["What is overdue this quarter"]'
              data-ht-widths='{"supplier": 240}'             this viewer's widths (app-persisted)
              data-ht-widths-style="#ledger-widths"          the app's server-rendered width <style>
              data-ht-off="drag pin resize overflow">       features the app keeps for itself
       <div data-ht-toolbar> … [data-ht-overflow="10"] … <details data-ht-more>…</details></div>
       <table id="ledger"> … </table>
     </section>
     <script src="/hub/components/table/table.js"></script>   (NOT deferred)
   ============================================================================= */
(function () {
  "use strict";

  var VERSION = "1.3.0";
  var doc = document;
  var mounts = Array.prototype.slice.call(doc.querySelectorAll("[data-hub-table]"));

  var PIN_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" aria-hidden="true">' +
    '<path d="M9 4h6l-1 5 3 3v2H7v-2l3-3zM12 14v6" stroke="currentColor" stroke-width="1.6" ' +
    'stroke-linecap="round" stroke-linejoin="round"/></svg>';
  var STAR_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" aria-hidden="true">' +
    '<path d="M12 3.2l2 5.3 5.3 2-5.3 2-2 5.3-2-5.3-5.3-2 5.3-2z" stroke="currentColor" ' +
    'stroke-width="1.5" stroke-linejoin="round"/></svg>';
  var X_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" aria-hidden="true">' +
    '<path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';

  /* ------------------------------------------------------------- small helpers */

  function el(tag, cls, text) {
    var node = doc.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }
  function recall(key) { try { return localStorage.getItem(key) || ""; } catch (e) { return ""; } }
  function remember(key, value) { try { localStorage.setItem(key, value); } catch (e) {} }
  function report(err) {
    // The error-visibility kit's hook: a failure here reaches the hub's error
    // stream like any other failure on the page. Never throws.
    try { if (window.reportProblem) window.reportProblem(err); } catch (e) {}
  }
  function announce(mount, name, detail) {
    var ev;
    try { ev = new CustomEvent("hub-table:" + name, { bubbles: true, cancelable: true, detail: detail }); }
    catch (e) { ev = doc.createEvent("CustomEvent"); ev.initCustomEvent("hub-table:" + name, true, true, detail); }
    return mount.dispatchEvent(ev);       // false when the app called preventDefault()
  }

  /* CSRF: <body data-csrf> first, a rendered csrfmiddlewaretoken input second,
     then THIS app's own cookie by exact name (<slug>_csrftoken), Django's default,
     and only last any name ending in csrftoken -- apps that share one host see each
     other's cookies, and a suffix match alone would send somebody else's secret. */
  function csrf(app) {
    var body = doc.body && doc.body.getAttribute("data-csrf");
    if (body) return body;
    var input = doc.querySelector('input[name="csrfmiddlewaretoken"]');
    if (input && input.value) return input.value;
    var jar = {}, hit = "";
    String(doc.cookie || "").split(";").forEach(function (part) {
      var eq = part.indexOf("=");
      if (eq < 0) return;
      var name = part.slice(0, eq).trim();
      if (!/csrftoken$/i.test(name)) return;
      jar[name] = hit = part.slice(eq + 1);
    });
    var own = String(app || "").replace(/-/g, "_") + "_csrftoken";
    var pick = jar[own] || jar.csrftoken || hit;
    return pick ? decodeURIComponent(pick) : "";
  }

  function postJSON(url, body, app, signal) {
    return fetch(url, {
      method: "POST", credentials: "same-origin", signal: signal,
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf(app), "Accept": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) {
      var type = r.headers.get("content-type") || "";
      // A JSON endpoint that answers HTML is a sign-in page or an error page.
      // Reading it as an empty answer is how a broken lane hides.
      if (type.indexOf("application/json") < 0) {
        var err = new Error("HTTP " + r.status + ": the app answered with a page, not data" +
                            (r.status === 200 || r.status === 302 ? " (signed out?)" : ""));
        err.status = r.status;
        throw err;
      }
      return r.json().then(function (data) { data.__status = r.status; return data; });
    });
  }

  function getJSON(url, signal) {
    return fetch(url, { credentials: "same-origin", signal: signal, headers: { "Accept": "application/json" } })
      .then(function (r) {
        var type = r.headers.get("content-type") || "";
        if (type.indexOf("application/json") < 0) {
          var err = new Error("HTTP " + r.status + ": the app answered with a page, not data");
          err.status = r.status;
          throw err;
        }
        return r.json().then(function (data) { data.__status = r.status; return data; });
      });
  }

  /* An app whose model lane is slower than a request should be held open
     answers {pending: true, poll: "<url>"} (HTTP 202) and finishes the work in
     the background. Poll it until the answer lands. Polling pauses while the
     tab is hidden (nobody is reading it) and resumes when it is shown again;
     the caller's AbortController ends it. An app that answers at once never
     reaches this. */
  function awaitAnswer(first, signal) {
    if (!first || !first.pending || !first.poll) return Promise.resolve(first);
    return new Promise(function (resolve, reject) {
      var timer = null;
      function onVisible() { if (!doc.hidden && timer === null) schedule(0); }
      function done(fn, v) { doc.removeEventListener("visibilitychange", onVisible); fn(v); }
      function schedule(ms) {
        timer = setTimeout(function () {
          timer = null;
          if (signal && signal.aborted) { var e = new Error("Stopped"); e.name = "AbortError"; return done(reject, e); }
          if (doc.hidden) return;              // resumes via visibilitychange
          getJSON(first.poll, signal).then(function (next) {
            if (next && next.pending) return schedule(Math.max(1000, next.after_ms || 2000));
            done(resolve, next);
          }, function (err) { done(reject, err); });
        }, ms);
      }
      doc.addEventListener("visibilitychange", onVisible);
      schedule(Math.max(1000, first.after_ms || 2000));
    });
  }

  /* =========================================================== one mount */

  function Table(mount) {
    this.mount = mount;
    var D = mount.dataset;
    this.app = D.app || "app";
    var sel = D.table || "";
    this.table = (sel && doc.querySelector(sel)) || mount.querySelector("table");
    var off = String(D.htOff || "").split(/[\s,]+/);
    this.off = {};
    for (var i = 0; i < off.length; i++) if (off[i]) this.off[off[i]] = true;
    this.key = "htable:" + this.app + ":" + (this.table && this.table.id || "table") + ":";
    if (this.table && !this.table.id) this.table.id = "ht-" + Math.random().toString(36).slice(2, 8);
    this.headRow = this.table && this.table.tHead && this.table.tHead.rows[0];
    this.scroller = this.table && (this.table.closest("[data-ht-scroll]") || this.table.closest(".table-scroll") ||
                                   this.table.parentElement);
    mount.setAttribute("data-ht-ready", VERSION);
    if (this.table && this.headRow) {
      if (!this.off.drag) this.bindDrag();
      if (!this.off.pin) this.bindPins();
      if (!this.off.drag || !this.off.pin) this.restore();
      if (!this.off.resize) this.bindResize();
      this.bindColumnHover();
    }
    if (!this.off.overflow) this.bindOverflow();
    if (D.shapeUrl) this.bindShape();
  }

  Table.prototype.claims = function (feature) { return !this.off[feature]; };

  /* ------------------------------------------------ 1. the heading is the handle */

  Table.prototype.fields = function () {
    var out = [];
    Array.prototype.forEach.call(this.headRow.cells, function (th) {
      out.push(th.getAttribute("data-field") || th.getAttribute("data-pin-field") || "");
    });
    return out;
  };

  Table.prototype.columnCells = function (index) {
    var cells = [];
    var width = this.headRow.cells.length;
    Array.prototype.forEach.call(this.table.rows, function (tr) {
      // A row whose cells do not line up with the header (a group row, an
      // expansion spanning the table) is not part of any column.
      if (tr.cells.length === width) cells.push(tr.cells[index]);
    });
    return cells;
  };

  /* Move column `from` to just before or after column `to`, in every row that
     lines up with the header. The page's own default when the app does not
     persist a layout itself. */
  Table.prototype.moveColumn = function (from, to, after) {
    var width = this.headRow.cells.length;
    if (from === to || from < 0 || to < 0) return;
    Array.prototype.forEach.call(this.table.rows, function (tr) {
      if (tr.cells.length !== width) return;
      var moving = tr.cells[from], target = tr.cells[to];
      tr.insertBefore(moving, after ? target.nextSibling : target);
    });
    if (this.widthStyle) this.paintWidths();
  };

  Table.prototype.bindDrag = function () {
    var self = this, head = this.headRow, from = null;
    function clear() {
      head.querySelectorAll(".ht-drop-before, .ht-drop-after").forEach(function (th) {
        th.classList.remove("ht-drop-before", "ht-drop-after");
      });
    }
    head.addEventListener("dragstart", function (ev) {
      var th = ev.target.closest && ev.target.closest("th[data-field][draggable]");
      // A drag that starts on a button (the pin) is a click target, not a column.
      if (!th || ev.target.closest("button")) { if (th) ev.preventDefault(); return; }
      from = th;
      th.classList.add("ht-dragging");
      ev.dataTransfer.effectAllowed = "move";
      // Clear first: a drag that began on the sort link would otherwise carry
      // the link's URL, and dropping it anywhere would navigate.
      try { ev.dataTransfer.clearData(); ev.dataTransfer.setData("text/plain", th.getAttribute("data-field")); } catch (e) {}
    });
    head.addEventListener("dragover", function (ev) {
      if (!from) return;
      var th = ev.target.closest && ev.target.closest("th[data-field]");
      if (!th || th === from) return;
      ev.preventDefault();
      clear();
      var box = th.getBoundingClientRect();
      th.classList.add((ev.clientX - box.left) > box.width / 2 ? "ht-drop-after" : "ht-drop-before");
    });
    head.addEventListener("drop", function (ev) {
      var th = ev.target.closest && ev.target.closest("th[data-field]");
      clear();
      if (!from || !th || th === from) { from = null; return; }
      ev.preventDefault();
      var box = th.getBoundingClientRect();
      var after = (ev.clientX - box.left) > box.width / 2;
      var fromIndex = Array.prototype.indexOf.call(head.cells, from);
      var toIndex = Array.prototype.indexOf.call(head.cells, th);
      var detail = { field: from.getAttribute("data-field"), target: th.getAttribute("data-field"),
                     place: after ? "after" : "before" };
      from.classList.remove("ht-dragging");
      from = null;
      if (announce(self.mount, "reorder", detail)) {
        self.moveColumn(fromIndex, toIndex, after);
        self.save();
        self.layoutPins();
      }
    });
    head.addEventListener("dragend", function () {
      if (from) from.classList.remove("ht-dragging");
      from = null;
      clear();
    });
  };

  /* ------------------------------------------------------- 2. pins on hover */

  Table.prototype.bindPins = function () {
    var self = this;
    // Every heading that can be pinned carries a pin. An app that drew its own
    // ([data-col-pin]) keeps it; the rest get one.
    Array.prototype.forEach.call(this.headRow.cells, function (th) {
      var field = th.getAttribute("data-pin-field") || th.getAttribute("data-field");
      if (!field || th.querySelector("[data-col-pin], [data-ht-pin]")) return;
      var pin = el("button", "ht-pin");
      pin.type = "button";
      pin.setAttribute("data-ht-pin", "");
      pin.setAttribute("aria-pressed", th.classList.contains("is-pinned") ? "true" : "false");
      var label = (th.textContent || field).trim();
      pin.title = (th.classList.contains("is-pinned") ? "Unpin " : "Pin ") + label +
                  (th.classList.contains("is-pinned") ? "" : " to the left edge");
      pin.innerHTML = PIN_SVG;
      th.classList.add("ht-has-pin");
      th.appendChild(pin);
    });
    this.headRow.addEventListener("click", function (ev) {
      var pin = ev.target.closest && ev.target.closest("[data-col-pin], [data-ht-pin]");
      if (!pin) return;
      ev.preventDefault();
      ev.stopPropagation();
      var th = pin.closest("th");
      var field = th.getAttribute("data-pin-field") || th.getAttribute("data-field");
      var pinned = !th.classList.contains("is-pinned");
      if (!announce(self.mount, "pin", { field: field, pinned: pinned })) {
        pin.disabled = true;               // the app is saving; its reload redraws this
        return;
      }
      var index = Array.prototype.indexOf.call(self.headRow.cells, th);
      self.columnCells(index).forEach(function (cell) { cell.classList.toggle("is-pinned", pinned); });
      pin.setAttribute("aria-pressed", pinned ? "true" : "false");
      // A held column has to BE at the left, or the columns between it and the
      // edge slide underneath it: pinned moves it to the end of the pinned block.
      if (pinned) {
        var last = -1;
        Array.prototype.forEach.call(self.headRow.cells, function (c, i) { if (i !== index && c.classList.contains("is-pinned")) last = i; });
        if (last + 1 !== index) self.moveColumn(index, last + 1 < 0 ? 0 : last + 1, false);
      }
      self.save();
      self.layoutPins();
    });

    this.pinStyle = el("style");
    this.pinStyle.setAttribute("data-ht-pins", this.table.id);
    doc.head.appendChild(this.pinStyle);
    var queued = false;
    function relayout() {
      if (queued) return;
      queued = true;
      requestAnimationFrame(function () { queued = false; self.layoutPins(); self.markScrolled(); });
    }
    this.relayout = relayout;
    if (this.scroller) this.scroller.addEventListener("scroll", function () { self.markScrolled(); }, { passive: true });
    window.addEventListener("resize", relayout);
    if (window.ResizeObserver) {
      var ro = new ResizeObserver(relayout);
      Array.prototype.forEach.call(this.headRow.cells, function (th) { ro.observe(th); });
    }
    if (doc.fonts && doc.fonts.ready) doc.fonts.ready.then(relayout);
    this.layoutPins();
    this.markScrolled();
  };

  /* Each pinned column's `left` is the sum of the pinned widths before it --
     known only once the browser has laid the table out -- written as ONE
     stylesheet, so a row drawn later (an expansion, an edit) is covered too. */
  Table.prototype.layoutPins = function () {
    if (!this.pinStyle) return;
    var left = 0, rules = [], last = -1, id = this.table.id;
    Array.prototype.forEach.call(this.headRow.cells, function (th, i) {
      if (!th.classList.contains("is-pinned")) return;
      rules.push("#" + id + " tr > .is-pinned:nth-child(" + (i + 1) + ") { left: " + left + "px; }");
      left += th.getBoundingClientRect().width;
      last = i;
    });
    this.pinStyle.textContent = rules.join("\n");
    this.table.querySelectorAll(".is-pin-edge").forEach(function (c) { c.classList.remove("is-pin-edge"); });
    if (last >= 0) {
      this.table.querySelectorAll("tr > .is-pinned:nth-child(" + (last + 1) + ")").forEach(function (c) {
        c.classList.add("is-pin-edge");
      });
    }
  };

  Table.prototype.markScrolled = function () {
    if (!this.scroller) return;
    var on = this.scroller.scrollLeft > 0;
    this.table.classList.toggle("is-scrolled-x", on);
  };

  /* The default persistence, for an app that did not take the events: this
     viewer's column order and pins, in this browser. A convenience and nothing
     more -- it is per device, and an app that wants a layout to follow a person
     saves it on the server and calls preventDefault(). */
  Table.prototype.save = function () {
    var order = this.fields();
    var pins = [];
    Array.prototype.forEach.call(this.headRow.cells, function (th) {
      if (th.classList.contains("is-pinned")) pins.push(th.getAttribute("data-pin-field") || th.getAttribute("data-field"));
    });
    remember(this.key + "layout", JSON.stringify({ order: order, pins: pins }));
  };

  Table.prototype.restore = function () {
    // Only for tables whose app does NOT persist layouts: an app that does says
    // so with data-ht-layout="server", and the server's drawing is the truth.
    if (this.mount.getAttribute("data-ht-layout") === "server") return;
    var raw = recall(this.key + "layout");
    if (!raw) return;
    var saved;
    try { saved = JSON.parse(raw); } catch (e) { return; }
    if (!saved || !Array.isArray(saved.order)) return;
    var self = this;
    saved.order.forEach(function (field, want) {
      var now = self.fields().indexOf(field);
      if (now >= 0 && want < self.headRow.cells.length && now !== want) self.moveColumn(now, want, false);
    });
    (saved.pins || []).forEach(function (field) {
      var i = self.fields().indexOf(field);
      if (i >= 0) self.columnCells(i).forEach(function (c) { c.classList.add("is-pinned"); });
    });
  };

  /* ------------------------------------------------ 6. every column can be sized

     Every heading carries a handle on its right edge: drag
     it, double-click it to hand the column back to its content, or focus it and
     use the arrow keys (Shift for bigger steps, Delete to reset). A column that
     was never sized is sized by its content, as before.

     The widths are painted into ONE style element -- the app's own
     (data-ht-widths-style, server-rendered so the first paint is already right)
     or one this component makes. The heading is keyed by FIELD, the body cells
     by the heading's CURRENT index, recomputed after every move; a row whose
     cells span columns (a group heading, an opened row) is left alone.

     A resize never reloads the page, and the handle never starts a column
     move: the heading's draggable is switched off for the length of the drag.
     The width is announced as hub-table:resize {field, width} (null = reset);
     an app that saves layouts itself calls preventDefault(). */
  var W_MIN = 48, W_MAX = 900;

  function fieldOf(th) { return th.getAttribute("data-field") || th.getAttribute("data-pin-field") || ""; }
  function attrSel(name, value) { return "[" + name + "=\"" + String(value).replace(/["\\]/g, "") + "\"]"; }

  Table.prototype.bindResize = function () {
    var self = this, D = this.mount.dataset;
    this.widths = {};
    try {
      var given = JSON.parse(D.htWidths || "{}");
      if (given && typeof given === "object") {
        Object.keys(given).forEach(function (f) {
          var px = Number(given[f]);
          if (isFinite(px) && px > 0) self.widths[f] = Math.max(W_MIN, Math.min(W_MAX, Math.round(px)));
        });
      }
    } catch (e) { report(e); }
    if (D.htLayout !== "server") {
      try {
        var mine = JSON.parse(recall(this.key + "widths") || "{}");
        if (mine && typeof mine === "object") Object.keys(mine).forEach(function (f) { self.widths[f] = mine[f]; });
      } catch (e) {}
    }
    this.widthStyle = (D.htWidthsStyle && doc.querySelector(D.htWidthsStyle)) || null;
    if (!this.widthStyle) {
      this.widthStyle = el("style");
      this.widthStyle.setAttribute("data-ht-widths", this.table.id);
      doc.head.appendChild(this.widthStyle);
    }

    Array.prototype.forEach.call(this.headRow.cells, function (th) {
      var field = fieldOf(th);
      if (!field || th.querySelector(".ht-resize")) return;
      var grip = el("span", "ht-resize");
      grip.setAttribute("role", "separator");
      grip.setAttribute("aria-orientation", "vertical");
      grip.setAttribute("tabindex", "0");
      grip.setAttribute("aria-label", "Resize " + ((th.textContent || field).trim() || field));
      grip.title = "Drag to resize \u00b7 double-click to fit the content";
      th.classList.add("ht-has-resize");
      th.appendChild(grip);

      var timer = null;
      function later() { clearTimeout(timer); timer = setTimeout(function () { self.commitWidth(field); }, 400); }
      function clamp(px) { return Math.max(W_MIN, Math.min(W_MAX, Math.round(px))); }

      grip.addEventListener("pointerdown", function (ev) {
        if (ev.button !== 0) return;
        ev.preventDefault();
        ev.stopPropagation();
        var draggable = th.getAttribute("draggable");
        th.removeAttribute("draggable");
        var floor = self.minWidthFor(th);
        self.freeze();
        var startX = ev.clientX, lastX = startX, startW = th.getBoundingClientRect().width, queued = false;
        try { grip.setPointerCapture(ev.pointerId); } catch (e) {}
        doc.documentElement.classList.add("ht-resizing");
        grip.classList.add("is-active");
        function move(e) {
          lastX = e.clientX;
          if (queued) return;
          queued = true;
          requestAnimationFrame(function () {
            queued = false;
            // ONE heading and the table's width per frame -- nothing else is
            // restyled while the pointer moves (rewriting a stylesheet with a
            // :has() row selector every frame, under auto layout, squeezed every
            // other column and read as a glitch).
            self.setLive(th, Math.max(floor, clamp(startW + (lastX - startX))));
          });
        }
        function up(e) {
          grip.removeEventListener("pointermove", move);
          grip.removeEventListener("pointerup", up);
          grip.removeEventListener("pointercancel", up);
          try { grip.releasePointerCapture(e.pointerId); } catch (x) {}
          doc.documentElement.classList.remove("ht-resizing");
          grip.classList.remove("is-active");
          if (draggable !== null) th.setAttribute("draggable", draggable);
          if (Math.abs(lastX - startX) >= 2) {
            self.widths[field] = Math.max(floor, clamp(startW + (lastX - startX)));
            self.setLive(th, self.widths[field]);
            self.paintWidths();
            self.commitWidth(field);
          }
        }
        grip.addEventListener("pointermove", move);
        grip.addEventListener("pointerup", up);
        grip.addEventListener("pointercancel", up);
      });
      // A click on the handle never sorts the column and never reaches the pin.
      grip.addEventListener("click", function (ev) { ev.preventDefault(); ev.stopPropagation(); });
      grip.addEventListener("dragstart", function (ev) { ev.preventDefault(); ev.stopPropagation(); });
      grip.addEventListener("dblclick", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        delete self.widths[field];
        self.unfreeze();
        self.paintWidths();
        self.commitWidth(field);
      });
      grip.addEventListener("keydown", function (ev) {
        var step = ev.shiftKey ? 64 : 16;
        if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") {
          ev.preventDefault();
          var now = self.widths[field] || th.getBoundingClientRect().width;
          self.freeze();
          self.widths[field] = Math.max(self.minWidthFor(th), clamp(now + (ev.key === "ArrowRight" ? step : -step)));
          self.setLive(th, self.widths[field]);
          self.paintWidths();
          later();
        } else if (ev.key === "Delete" || ev.key === "Backspace") {
          ev.preventDefault();
          delete self.widths[field];
          self.unfreeze();
          self.paintWidths();
          later();
        }
      });
    });
    this.paintWidths();
  };

  Table.prototype.paintWidths = function () {
    if (!this.widthStyle) return;
    var id = this.table.id, widths = this.widths || {}, rules = [];
    var self = this;
    Array.prototype.forEach.call(this.headRow.cells, function (th, i) {
      var field = fieldOf(th), px = field && widths[field];
      if (!px) return;
      px = Math.max(px, self.minWidthFor(th));
      // border-box: the number a person dragged to is the width they SEE,
      // padding and rule included (content-box added ~21px to every drag).
      var box = " { box-sizing: border-box; width: " + px + "px; min-width: " + px + "px; max-width: " + px +
                "px; overflow: hidden; text-overflow: ellipsis; }";
      rules.push("#" + id + " thead th" + attrSel("data-field", field) + ", #" + id + " thead th" +
                 attrSel("data-pin-field", field) + ", #" + id + " tbody tr:not(:has(> [colspan])) > :nth-child(" +
                 (i + 1) + ")" + box);
    });
    this.widthStyle.textContent = rules.join("\n");
    // Pinned columns sit at the sum of the widths before them.
    if (this.relayout) this.relayout(); else this.layoutPins();
  };

  /* THE FLOOR IS THE HEADING'S OWN ROOM: padding + the label as drawn + the sort
     arrow + the pin and its gap, measured from the heading, never a constant --
     so no column can be dragged narrower than the words and the pin above it. */
  var PIN_ROOM = 22 + 10;
  Table.prototype.minWidthFor = function (th) {
    var cs = window.getComputedStyle(th);
    var room = (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0) + PIN_ROOM;
    var label = 0;
    Array.prototype.forEach.call(th.childNodes, function (node) {
      if (node.nodeType === 1) {
        if (node.matches(".ht-resize, .ht-pin, [data-col-pin], [data-ht-pin]")) return;
        label += node.getBoundingClientRect().width;
      } else if (node.nodeType === 3 && node.textContent.trim()) {
        var r = doc.createRange();
        r.selectNodeContents(node);
        label += r.getBoundingClientRect().width;
      }
    });
    if (th.hasAttribute("aria-sort")) label += 14;
    return Math.max(W_MIN, Math.ceil(room + label));
  };

  /* FREEZE ONCE, THEN MOVE ONE THING. The first resize locks every heading at
     the width it is drawn at and switches the table to fixed layout, so a drag
     changes the dragged column and the table's width and nothing else -- the
     neighbours stay exactly where the eye left them. Not persisted: a reload
     is sized by content plus the widths people chose. */
  Table.prototype.freeze = function () {
    if (this.frozen) return;
    var total = 0;
    Array.prototype.forEach.call(this.headRow.cells, function (th) {
      var w = th.getBoundingClientRect().width;
      th.style.width = th.style.minWidth = th.style.maxWidth = w + "px";
      total += w;
    });
    this.table.style.tableLayout = "fixed";
    this.table.style.width = total + "px";
    this.table.style.minWidth = "0";
    this.table.classList.add("ht-fixed");
    this.frozen = true;
  };
  Table.prototype.unfreeze = function () {
    if (!this.frozen) return;
    Array.prototype.forEach.call(this.headRow.cells, function (th) {
      th.style.width = th.style.minWidth = th.style.maxWidth = "";
    });
    this.table.style.tableLayout = this.table.style.width = this.table.style.minWidth = "";
    this.table.classList.remove("ht-fixed");
    this.frozen = false;
    if (this.relayout) this.relayout();
  };
  Table.prototype.setLive = function (th, px) {
    var before = th.getBoundingClientRect().width;
    th.style.width = th.style.minWidth = th.style.maxWidth = px + "px";
    var w = parseFloat(this.table.style.width) || this.table.getBoundingClientRect().width;
    this.table.style.width = Math.round(w + (px - before)) + "px";
    var field = fieldOf(th);
    if (field) this.widths[field] = px;
    if (this.relayout) this.relayout(); else this.layoutPins();
  };

  /* THE PIN SHOWS FOR THE COLUMN UNDER THE POINTER. Pointing at
     any body cell marks its heading .ht-col-hot, which the pin rule reads --
     one class on one heading, moved on pointerover, cleared on leaving. */
  Table.prototype.bindColumnHover = function () {
    var self = this, hot = null;
    function set(th) {
      if (th === hot) return;
      if (hot) hot.classList.remove("ht-col-hot");
      hot = th;
      if (hot) hot.classList.add("ht-col-hot");
    }
    var body = this.table.tBodies[0];
    if (!body) return;
    body.addEventListener("pointerover", function (ev) {
      var cell = ev.target.closest && ev.target.closest("td, th");
      if (!cell || cell.colSpan > 1 || !body.contains(cell)) return set(null);
      set(self.headRow.cells[cell.cellIndex] || null);
    });
    this.table.addEventListener("pointerleave", function () { set(null); });
  };

  Table.prototype.commitWidth = function (field) {
    var width = (this.widths && this.widths[field]) || null;
    if (!announce(this.mount, "resize", { field: field, width: width })) return;   // the app persists
    if (this.mount.getAttribute("data-ht-layout") === "server") return;
    remember(this.key + "widths", JSON.stringify(this.widths || {}));
  };

  /* ------------------------------------------ 3. the band never wraps */

  Table.prototype.bindOverflow = function () {
    var top = this.mount.querySelector("[data-ht-toolbar]");
    var more = top && top.querySelector("[data-ht-more]");
    var moved = more && more.querySelector("[data-ht-more-moved]");
    if (!top || !more || !moved) return;
    var count = more.querySelector("[data-ht-more-n]");
    var scope = top.querySelector("[data-ht-scope]") || top;
    var folded = more.querySelectorAll("[data-ht-folded]").length;

    var items = [];
    top.querySelectorAll("[data-ht-overflow]").forEach(function (node) {
      items.push({ el: node, rank: Number(node.getAttribute("data-ht-overflow")) || 0 });
    });
    // Tiles go from the RIGHT end, and never the one the person is on.
    var tiles = Array.prototype.slice.call(top.querySelectorAll("[data-ht-overflow-tile]"));
    tiles.slice().reverse().forEach(function (tile, i) {
      if (!tile.classList.contains("is-on") && tile.getAttribute("aria-current") !== "true") {
        items.push({ el: tile, rank: 40 + i });
      }
    });
    var domOrder = Array.prototype.slice.call(top.querySelectorAll("[data-ht-overflow], [data-ht-overflow-tile]"));
    items.forEach(function (it) {
      it.home = doc.createComment("ht-home");
      it.el.parentNode.insertBefore(it.home, it.el);
      it.order = domOrder.indexOf(it.el);
      it.out = false;
    });
    var byRank = items.slice().sort(function (a, b) { return a.rank - b.rank; });
    var seps = Array.prototype.slice.call(scope.querySelectorAll("[data-ht-sep]"));

    function tidy() {
      // A hairline between two groups means nothing once a group has left.
      seps.forEach(function (sep) {
        var before = false, after = false, n;
        for (n = sep.previousElementSibling; n; n = n.previousElementSibling) {
          if (n.hasAttribute("data-ht-overflow-tile")) { before = true; break; }
        }
        for (n = sep.nextElementSibling; n; n = n.nextElementSibling) {
          if (n.hasAttribute("data-ht-overflow-tile")) { after = true; break; }
        }
        sep.hidden = !(before && after);
      });
    }
    function overflowing() {
      return scope.scrollWidth > scope.clientWidth + 1 || top.scrollWidth > top.clientWidth + 1;
    }
    function place() {
      var out = items.filter(function (it) { return it.out; }).sort(function (a, b) { return a.order - b.order; });
      out.forEach(function (it) { moved.appendChild(it.el); });
      moved.hidden = out.length === 0;
      var n = out.length + folded;
      more.hidden = n === 0;
      if (count) count.textContent = String(n);
    }
    var lastWidth = -1;
    function fit(force) {
      var width = top.clientWidth;
      if (!force && width === lastWidth) return;
      lastWidth = width;
      items.forEach(function (it) {
        if (it.out) { it.home.parentNode.insertBefore(it.el, it.home.nextSibling); it.out = false; }
      });
      more.hidden = folded === 0;
      tidy();
      if (!overflowing()) { place(); return; }
      more.hidden = false;                 // the menu's own button takes room too
      for (var i = 0; i < byRank.length && overflowing(); i++) {
        byRank[i].el.parentNode.removeChild(byRank[i].el);
        byRank[i].out = true;
        tidy();
      }
      place();
    }
    var queued = false;
    function queue() {
      if (queued) return;
      queued = true;
      requestAnimationFrame(function () { queued = false; fit(false); });
    }
    fit(true);
    if (window.ResizeObserver) new ResizeObserver(queue).observe(top);
    else window.addEventListener("resize", queue);
    if (doc.fonts && doc.fonts.ready) doc.fonts.ready.then(function () { fit(true); });
    this.refit = function () { fit(true); };

    moved.addEventListener("click", function (ev) {
      if (ev.target.closest("a, button:not([data-ht-keep-open])")) more.open = false;
    });
    doc.addEventListener("click", function (ev) {
      if (more.open && !more.contains(ev.target)) more.open = false;
    });
    doc.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && more.open) {
        more.open = false;
        var s = more.querySelector("summary");
        if (s) s.focus();
      }
    });
  };

  /* ------------------------------------------------ 4 + 5. suggest, request it */

  Table.prototype.bindShape = function () {
    var self = this, D = this.mount.dataset;
    var button = this.mount.querySelector("[data-ht-shape]");
    if (!button) {
      var bar = this.mount.querySelector("[data-ht-toolbar]");
      if (!bar) return;
      button = el("button", "ht-shape-btn");
      button.type = "button";
      button.setAttribute("data-ht-shape", "");
      button.textContent = "Suggest";
      var moreMenu = bar.querySelector("[data-ht-more]");
      (moreMenu && moreMenu.parentNode === bar) ? bar.insertBefore(button, moreMenu) : bar.appendChild(button);
    }
    button.hidden = false;
    button.setAttribute("aria-expanded", "false");
    if (D.agentPowered != null && !button.querySelector(".ht-star")) {
      var star = el("span", "ht-star");
      star.setAttribute("aria-hidden", "true");
      star.innerHTML = STAR_SVG;
      button.insertBefore(star, button.firstChild);
      button.title = "Agent powered: suggests cuts of this table";
    }
    this.shapeButton = button;
    button.addEventListener("click", function (ev) {
      ev.preventDefault();
      if (self.panel && !self.panel.hidden) self.closeShape(); else self.openShape();
    });
    if (this.refit) this.refit();
  };

  Table.prototype.buildPanel = function () {
    var self = this, D = this.mount.dataset;
    var panel = el("div", "ht-shape");
    panel.setAttribute("role", "region");
    panel.setAttribute("aria-label", "Suggest cuts of this table");
    panel.hidden = true;

    var head = el("div", "ht-shape__head");
    var title = el("div", "ht-shape__title");
    if (D.agentPowered != null) {
      var star = el("span", "ht-star");
      star.setAttribute("role", "img");
      star.setAttribute("aria-label", "Agent powered");
      star.innerHTML = STAR_SVG;
      title.appendChild(star);
    }
    title.appendChild(el("b", null, "Shape this table"));
    title.appendChild(el("span", "ht-shape__sub",
      "Say what you are trying to do. You get cuts of this table, each with the rows it would show."));
    var close = el("button", "ht-shape__close");
    close.type = "button";
    close.setAttribute("aria-label", "Close");
    close.innerHTML = X_SVG;
    close.addEventListener("click", function () { self.closeShape(); });
    head.appendChild(title);
    head.appendChild(close);

    var form = el("form", "ht-shape__ask");
    var input = el("input", "ht-shape__input");
    input.type = "text";
    input.name = "goal";
    input.maxLength = 600;
    input.autocomplete = "off";
    input.placeholder = D.shapePlaceholder || "e.g. what is overdue and has no owner yet";
    input.setAttribute("aria-label", "What are you trying to do?");
    input.value = recall(this.key + "goal");
    input.addEventListener("input", function () { remember(self.key + "goal", input.value); });
    var go = el("button", "ht-btn ht-btn--primary", "Suggest");
    go.type = "submit";
    form.appendChild(input);
    form.appendChild(go);
    form.addEventListener("submit", function (ev) { ev.preventDefault(); self.suggest(input.value); });

    var examples = [];
    try { examples = JSON.parse(D.shapeExamples || "[]"); } catch (e) { examples = []; }
    var hints = el("div", "ht-shape__hints");
    if (examples.length) {
      hints.appendChild(el("span", "ht-shape__hint-k", "Try"));
      examples.slice(0, 4).forEach(function (text) {
        var b = el("button", "ht-hint", String(text));
        b.type = "button";
        b.addEventListener("click", function () { input.value = String(text); remember(self.key + "goal", input.value); self.suggest(input.value); });
        hints.appendChild(b);
      });
    }
    var blank = el("button", "ht-hint ht-hint--quiet", "Nothing in mind: what is worth a look?");
    blank.type = "button";
    blank.addEventListener("click", function () { input.value = ""; remember(self.key + "goal", ""); self.suggest(""); });
    hints.appendChild(blank);

    var body = el("div", "ht-shape__body");
    body.setAttribute("aria-live", "polite");

    panel.appendChild(head);
    panel.appendChild(form);
    panel.appendChild(hints);
    panel.appendChild(body);
    panel.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") { ev.preventDefault(); self.closeShape(); }
    });

    var bar = this.mount.querySelector("[data-ht-toolbar]");
    var host = this.mount.querySelector("[data-ht-shape-host]");
    if (host) host.appendChild(panel);
    else if (bar && bar.parentNode) bar.parentNode.insertBefore(panel, bar.nextSibling);
    else this.mount.insertBefore(panel, this.mount.firstChild);
    this.panel = panel;
    this.input = input;
    this.body = body;
    return panel;
  };

  Table.prototype.openShape = function () {
    if (!this.panel) this.buildPanel();
    this.panel.hidden = false;
    this.shapeButton.setAttribute("aria-expanded", "true");
    this.input.focus();
    this.input.select();
  };

  Table.prototype.closeShape = function () {
    if (this.pending) { try { this.pending.abort(); } catch (e) {} this.pending = null; }
    if (this.panel) this.panel.hidden = true;
    this.shapeButton.setAttribute("aria-expanded", "false");
    this.shapeButton.focus();
  };

  Table.prototype.state = function (kind, words, extra) {
    this.body.textContent = "";
    var box = el("div", "ht-state ht-state--" + kind);
    box.appendChild(el("p", null, words));
    if (extra) box.appendChild(extra);
    this.body.appendChild(box);
    return box;
  };

  Table.prototype.currentFilters = function () {
    var out = {};
    try {
      new URLSearchParams(location.search).forEach(function (v, k) { if (v) out[k] = v; });
    } catch (e) {}
    return out;
  };

  Table.prototype.suggest = function (goal) {
    var self = this, D = this.mount.dataset;
    if (!this.panel) this.buildPanel();
    if (this.pending) { try { this.pending.abort(); } catch (e) {} }
    var ctrl = window.AbortController ? new AbortController() : null;
    this.pending = ctrl;
    var started = Date.now();
    var stop = el("button", "ht-btn ht-btn--quiet", "Stop");
    stop.type = "button";
    stop.addEventListener("click", function () { if (ctrl) ctrl.abort(); });
    var box = this.state("loading", goal ? "Reading the table for: “" + goal + "”" : "Reading the table for what is worth a look", stop);
    var clock = el("span", "ht-clock", "0s");
    box.firstChild.appendChild(doc.createTextNode(" "));
    box.firstChild.appendChild(clock);
    var tick = setInterval(function () { clock.textContent = Math.round((Date.now() - started) / 1000) + "s"; }, 1000);
    // A shared model queue was measured at 16 s for one answer and 130 s for the
    // same prompt minutes later, so the app may work in the background (202 +
    // poll) and this waits up to five minutes. A hung lane must still end.
    var LIMIT_MS = 300000;
    var limit = setTimeout(function () { if (ctrl) ctrl.abort(); }, LIMIT_MS);
    var kind = D.kind || this.currentFilters().kind || "";
    postJSON(D.shapeUrl, { goal: goal || "", kind: kind, current: this.currentFilters() }, this.app, ctrl && ctrl.signal)
      .then(function (first) {
        if (first && first.pending) {
          var note = box.querySelector(".ht-state__note") || el("span", "ht-state__note");
          note.textContent = "The model's queue is shared; this can take a minute or two. You can keep working.";
          if (!note.parentNode) box.firstChild.appendChild(note);
        }
        return awaitAnswer(first, ctrl && ctrl.signal);
      })
      .then(function (answer) {
        if (!answer.ok) {
          self.state(answer.code === "unconfigured" ? "off" : "failed",
                      answer.reason || "The suggestion lane failed (HTTP " + answer.__status + ").");
          return;
        }
        self.lastAnswer = answer;
        self.showAnswer(answer);
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") {
          self.state("off", Date.now() - started >= LIMIT_MS - 1000
            ? "No answer in five minutes, so it was stopped. The model's queue is shared; try again shortly."
            : "Stopped. Nothing on the table changed.");
          return;
        }
        report(err);
        self.state("failed", "Could not get suggestions: " + (err && err.message || err) + ". It has been reported.");
      })
      .then(function () {
        clearInterval(tick);
        clearTimeout(limit);
        if (self.pending === ctrl) self.pending = null;
      });
  };

  /* Draw an answer. Public (HubTable.showAnswer) so an app -- or a person
     checking the renderer against a real answer -- can hand one in. */
  Table.prototype.showAnswer = function (answer) {
    var self = this, D = this.mount.dataset;
    if (!this.panel) this.buildPanel();
    this.panel.hidden = false;
    this.body.textContent = "";
    var lenses = Array.isArray(answer.lenses) ? answer.lenses : [];

    var meta = el("p", "ht-shape__meta");
    meta.textContent = lenses.length
      ? lenses.length + " cut" + (lenses.length === 1 ? "" : "s") + " of " + (answer.rows != null ? answer.rows + " rows" : "the table") +
        (answer.goal ? " for “" + answer.goal + "”" : ", worth a look") +
        (answer.ms ? " · " + (answer.ms / 1000).toFixed(1) + "s" : "")
      : "No cut of this table fits that.";
    this.body.appendChild(meta);

    if (lenses.length) {
      var grid = el("div", "ht-lenses");
      lenses.forEach(function (lens) {
        var card = el("article", "ht-lens" + (lens.count === 0 ? " is-empty" : ""));
        var top = el("div", "ht-lens__top");
        top.appendChild(el("h4", "ht-lens__title", lens.title || "Untitled"));
        var n = el("span", "ht-lens__n");
        n.appendChild(el("b", null, String(lens.count != null ? lens.count : "?")));
        n.appendChild(doc.createTextNode(" of " + (lens.of != null ? lens.of : "?")));
        n.title = "Rows this cut shows, counted by the app with the table's own filter";
        top.appendChild(n);
        card.appendChild(top);
        if (lens.why) card.appendChild(el("p", "ht-lens__why", lens.why));
        var chips = el("div", "ht-lens__chips");
        (lens.chips || []).forEach(function (c) { chips.appendChild(el("span", "ht-chip", c)); });
        (lens.column_words || []).forEach(function (c) { chips.appendChild(el("span", "ht-chip ht-chip--cols", c)); });
        if (chips.childNodes.length) card.appendChild(chips);
        var act = el("div", "ht-lens__act");
        var open = el("a", "ht-btn ht-btn--primary", lens.count === 0 ? "Open (empty now)" : "Open");
        open.href = lens.url || "?";
        act.appendChild(open);
        var cols = lens.columns || {};
        if ((cols.show && cols.show.length) || (cols.hide && cols.hide.length) || (cols.pin && cols.pin.length)) {
          var withCols = el("button", "ht-btn", "Open with these columns");
          withCols.type = "button";
          withCols.title = (lens.column_words || []).join("; ");
          withCols.addEventListener("click", function () {
            withCols.disabled = true;
            withCols.textContent = "Arranging…";
            if (announce(self.mount, "layout", { columns: cols, url: lens.url })) {
              location.href = lens.url || location.href;
            }
          });
          act.appendChild(withCols);
        }
        card.appendChild(act);
        grid.appendChild(card);
      });
      this.body.appendChild(grid);
    }

    if (answer.gap) {
      var gap = el("div", "ht-gap");
      gap.appendChild(el("b", null, "The table cannot do all of this yet. "));
      gap.appendChild(doc.createTextNode(answer.gap));
      this.body.appendChild(gap);
    }
    if (answer.refused && answer.refused.length) {
      var fold = el("details", "ht-refused");
      fold.appendChild(el("summary", null, answer.refused.length + " thing" +
        (answer.refused.length === 1 ? "" : "s") + " the model named that this table does not have"));
      var list = el("ul");
      answer.refused.slice(0, 12).forEach(function (r) { list.appendChild(el("li", null, r)); });
      fold.appendChild(list);
      this.body.appendChild(fold);
    }

    var canAsk = D.requestUrl && (answer.can_request === true || (answer.can_request == null && D.canRequest === "1"));
    if (canAsk) this.body.appendChild(this.requestIt(answer));
    else if (D.requestUrl) {
      this.body.appendChild(el("p", "ht-shape__foot",
        "Need something the table cannot do? A contributor can file it as a request."));
    }
  };

  Table.prototype.requestIt = function (answer) {
    var self = this, D = this.mount.dataset;
    var foot = el("div", "ht-request");
    var words = el("div", "ht-request__words");
    words.appendChild(el("b", null, answer.gap ? "Ask for it to be built" : "None of these?"));
    words.appendChild(el("span", null, " Your words go on the board as written, as work to build it into this table."));
    var btn = el("button", "ht-btn" + (answer.gap ? " ht-btn--primary" : ""), "Request it");
    btn.type = "button";
    foot.appendChild(words);
    foot.appendChild(btn);
    btn.addEventListener("click", function () {
      var goal = (self.input && self.input.value || answer.goal || "").trim();
      if (goal.length < 8) {
        self.input.focus();
        words.lastChild.textContent = " Say what you need in a sentence first, in the box above.";
        return;
      }
      btn.disabled = true;
      btn.textContent = "Filing…";
      postJSON(D.requestUrl, { goal: goal, lenses: answer.lenses || [], gap: answer.gap || "",
                               current_query: location.search }, self.app)
        .then(function (res) {
          foot.textContent = "";
          if (!res.ok) {
            foot.className = "ht-request ht-request--failed";
            foot.appendChild(el("span", null, res.reason || "The ask was not filed (HTTP " + res.__status + ")."));
            return;
          }
          foot.className = "ht-request ht-request--done";
          foot.appendChild(el("b", null, "Asked. "));
          foot.appendChild(doc.createTextNode("Filed as " + res.task + "; it is on the board for whoever picks it up. "));
          if (res.board) {
            var a = el("a", null, "See it on the board");
            a.href = res.board;
            a.target = "_blank";
            a.rel = "noopener";
            foot.appendChild(a);
          }
        })
        .catch(function (err) {
          report(err);
          btn.disabled = false;
          btn.textContent = "Request it";
          words.lastChild.textContent = " Could not file it: " + (err && err.message || err) + ". It has been reported.";
        });
    });
    return foot;
  };

  /* =============================================================== boot */

  var tables = [];
  mounts.forEach(function (mount) {
    if (mount.getAttribute("data-ht-ready")) return;
    try { tables.push(new Table(mount)); }
    catch (err) { report(err); }
  });

  function find(node) {
    for (var i = 0; i < tables.length; i++) {
      var t = tables[i];
      if (node === t.mount || node === t.table || t.mount.contains(node)) return t;
    }
    return null;
  }

  window.HubTable = {
    version: VERSION,
    /* Does the component own `feature` for the table (or toolbar) `node`? An
       app keeps a fallback for each feature (drag | pin | resize | overflow) and runs it only when this says
       no -- so a page whose hub link failed still works exactly as before. */
    claims: function (node, feature) { var t = node && find(node); return !!(t && t.claims(feature)); },
    showAnswer: function (node, answer) { var t = find(typeof node === "string" ? doc.querySelector(node) : node); if (t) t.showAnswer(answer); },
    relayout: function (node) { var t = find(node); if (t && t.relayout) t.relayout(); }
  };
})();
