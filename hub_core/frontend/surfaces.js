/* The Hub's shared record-reading and live-reconcile surface. No dependencies, no remote assets.
 *
 * Two jobs, both about not lying to the reader:
 *
 *   1. READ A RECORD WHOLE. A detail dialog that renders only the fields its author thought of
 *      silently hides every field added later (a component's `get` and `when`, a gap's
 *      evidence). `fields()` renders whatever the record carries that the caller has not already
 *      shown: arrays as lists, objects as nested definition lists (never "[object Object]" or a
 *      JSON repr), markdown-ish prose as paragraphs/lists/code, record ids as buttons that open
 *      that record, and ONLY http(s) URLs as links. Record text never becomes HTML or script —
 *      every node is built with textContent.
 *
 *   2. PATCH, DON'T REPLACE. A live update that rebuilds a list throws away the reader's place:
 *      focus, scroll, an expanded <details>, a running animation. `reconcile(target, source)`
 *      walks a freshly built tree against the attached one, keyed by record identity
 *      (data-entity-id / data-task-id / data-live-key / id), keeps every unchanged node
 *      attached, patches changed attributes and text in place, and briefly outlines a keyed node
 *      whose data-record-version moved (skipped under prefers-reduced-motion or a hidden tab).
 */
(function (global) {
  "use strict";
  var doc = global.document;

  function node(tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  }
  function button(text, action, cls) {
    var n = node("button", cls || "text-action", text);
    n.type = "button";
    n.addEventListener("click", function (e) { e.stopPropagation(); action(e); });
    return n;
  }
  var NAMES = {
    body_md: "Detail", notes_md: "Notes", context_md: "Context", decision_md: "Decision",
    consequences_md: "Consequences", evidence_uri: "Evidence", sha: "Commit",
    served_sha: "Running commit", iface: "Interface", remediation_cmd: "Remediation command",
    depends_on: "Depends on", applies_all: "Applies every component", hosted_at: "Hosted at",
    realized_by: "Realized by", consumed_by: "Consumed by", tasks_closed: "Tasks closed"
  };
  function label(key) {
    return NAMES[key] || String(key).replace(/_/g, " ").replace(/^./, function (c) { return c.toUpperCase(); });
  }
  function present(v) {
    return v != null && v !== "" && (!Array.isArray(v) || v.length > 0) &&
      (typeof v !== "object" || Array.isArray(v) || Object.keys(v).length > 0);
  }

  // A record id looks like <project>:<type>:<local>; any type the board knows opens in place.
  var ID_RE = "[a-z][\\w.-]*:(?:task|adr|feat|gap|cap|deploy|note|directive|ack):[\\w.-]+";
  function inline(text, ctx) {
    var span = node("span");
    var re = new RegExp("\\[([^\\]\\n]+)\\]\\((https?:\\/\\/[^\\s)]+)\\)|(https?:\\/\\/[^\\s<>]+)|\\b(" + ID_RE +
                        ")|(`[^`\\n]+`)|(\\*\\*[^*\\n]+\\*\\*)", "g");
    var s = String(text), cursor = 0, m;
    while ((m = re.exec(s))) {
      span.appendChild(doc.createTextNode(s.slice(cursor, m.index)));
      if (m[5]) span.appendChild(node("code", "record-inline-code", m[5].slice(1, -1)));
      else if (m[6]) { var strong = node("strong"); strong.appendChild(inline(m[6].slice(2, -2), ctx)); span.appendChild(strong); }
      else if (m[4]) {
        if (ctx && ctx.open) {
          span.appendChild(button((ctx.title && ctx.title(m[4])) || m[4],
            (function (id) { return function () { ctx.open(id); }; })(m[4]), "record-ref"));
        } else span.appendChild(node("code", "record-inline-code", m[4]));
      } else {
        var url = m[2] || m[3], trailing = "";
        if (!m[2]) { trailing = (url.match(/[.,;:)]+$/) || [""])[0]; url = url.slice(0, url.length - trailing.length); }
        var a = node("a", "record-link", m[1] || url);
        a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer";
        span.appendChild(a);
        if (trailing) span.appendChild(doc.createTextNode(trailing));
      }
      cursor = re.lastIndex;
    }
    span.appendChild(doc.createTextNode(s.slice(cursor)));
    return span;
  }

  function prose(text, ctx) {
    var body = node("div", "record-prose"), code = false, codeLines = [], list = null, paragraph = [];
    function flush() {
      if (!paragraph.length) return;
      var p = node("p"); p.appendChild(inline(paragraph.join(" "), ctx)); body.appendChild(p); paragraph = [];
    }
    String(text).split(/\r?\n/).forEach(function (line) {
      if (/^\s*```/.test(line)) {
        flush();
        if (code) { body.appendChild(node("pre", "record-code", codeLines.join("\n"))); codeLines = []; }
        code = !code; list = null; return;
      }
      if (code) { codeLines.push(line); return; }
      if (!line.trim()) { flush(); list = null; return; }
      var heading = line.match(/^\s*#{1,6}\s+(.+)$/), item = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.+)$/);
      if (heading) { flush(); var h = node("h4"); h.appendChild(inline(heading[1], ctx)); body.appendChild(h); list = null; }
      else if (item) {
        flush();
        if (!list) { list = node("ul", "record-list"); body.appendChild(list); }
        var li = node("li"); li.appendChild(inline(item[1], ctx)); list.appendChild(li);
      } else { list = null; paragraph.push(line.trim()); }
    });
    flush();
    if (codeLines.length) body.appendChild(node("pre", "record-code", codeLines.join("\n")));
    return body;
  }

  function value(v, ctx, depth) {
    depth = depth || 0;
    if (v == null || v === "") return node("span", "record-absent", "Not recorded");
    if (typeof v === "boolean") return node("span", "", v ? "Yes" : "No");
    if (typeof v === "number") return node("span", "mono", String(v));
    if (Array.isArray(v)) {
      var list = node("ul", "record-list");
      v.forEach(function (entry) { var li = node("li"); li.appendChild(value(entry, ctx, depth + 1)); list.appendChild(li); });
      return list;
    }
    if (typeof v === "object") {
      if (depth > 6) return node("span", "record-absent", "Nested too deeply to show here");
      var dl = node("dl", "record-values");
      Object.keys(v).forEach(function (k) {
        if (k.charAt(0) === "_") return;
        dl.appendChild(node("dt", "", label(k)));
        var dd = node("dd"); dd.appendChild(value(v[k], ctx, depth + 1)); dl.appendChild(dd);
      });
      return dl;
    }
    return prose(v, ctx);
  }

  // Fields that are bookkeeping rather than content: shown, but folded under "Record details".
  var ADMIN = ["legacy_ref", "owner", "lease_expires", "held_for_s", "agent", "actor", "recorded_by",
               "created_at", "updated_at", "idem_key", "ready", "blocked", "unblocked", "deps_blocked",
               "blocks_count", "urgency", "pickup_rank", "planning_state", "work_kind"];

  /* Everything `record` carries that `shown` (a list of keys the caller already rendered) does
     not name. Returns {content: Node|null, admin: Node|null}. */
  function fields(record, shown, ctx) {
    var skip = {}, content = node("div", "record-fields"), admin = {};
    (shown || []).concat(["id", "type", "title", "name", "version", "provenance"]).forEach(function (k) { skip[k] = true; });
    Object.keys(record || {}).forEach(function (k) {
      if (skip[k] || k.charAt(0) === "_" || !present(record[k])) return;
      if (ADMIN.indexOf(k) >= 0) { admin[k] = record[k]; return; }
      var part = node("section", "record-field");
      part.appendChild(node("h4", "record-field-label", label(k)));
      part.appendChild(value(record[k], ctx));
      content.appendChild(part);
    });
    var adminNode = null;
    if (Object.keys(admin).length) {
      adminNode = node("details", "record-metadata");
      adminNode.appendChild(node("summary", "", "Record details"));
      adminNode.appendChild(value(admin, ctx));
    }
    return { content: content.children.length ? content : null, admin: adminNode };
  }

  /* A task's plan as a checkpoint timeline, keeping every reported note — the trail a reader
     came for. A progress bar alone says "3/5" and hides what the worker actually reported. */
  function checkpoints(plan, ctx) {
    var steps = node("ol", "record-timeline"), done = 0;
    (plan || []).forEach(function (step, i) {
      step = step || {};
      if (step.done) done += 1;
      var li = node("li", step.done ? "is-done" : "");
      li.appendChild(node("span", "record-step-marker", step.done ? "✓" : String(i + 1)));
      var body = node("div");
      body.appendChild(value(step.step || step.title || ("Checkpoint " + (i + 1)), ctx));
      if (step.note) {
        var note = node("div", "record-step-note");
        note.appendChild(value(step.note, ctx));
        if (step.note_at) note.appendChild(node("div", "cell-sub mono", step.note_at));
        body.appendChild(note);
      }
      li.appendChild(body); steps.appendChild(li);
    });
    return { node: steps, done: done, total: (plan || []).length };
  }

  /* ---------------------------- live reconcile ---------------------------- */
  function key(n) {
    return (n && n.dataset && (n.dataset.entityId || n.dataset.taskId || n.dataset.liveKey)) ||
           (n && n.id) || "";
  }
  function reduced() {
    return !!(global.matchMedia && global.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }
  function compatible(a, b) {
    return !!a && a.nodeType === b.nodeType && a.nodeName === b.nodeName && key(a) === key(b) &&
      (a.nodeType !== 1 || !!key(a) || a.classList.item(0) === b.classList.item(0));
  }
  function patch(target, source, animate) {
    if (target.nodeType !== 1) { if (target.nodeValue !== source.nodeValue) target.nodeValue = source.nodeValue; return target; }
    if (target.isEqualNode(source)) return target;
    // An unkeyed control carries a closure built for the fresh render; take the fresh one
    // unless the reader is on it right now.
    if (!key(target) && target.matches("button,a,input,textarea,select,[role=\"button\"]")) {
      if (target === doc.activeElement) return target;
      target.replaceWith(source); return source;
    }
    var top = target.scrollTop, left = target.scrollLeft, expanded = target.open;
    var moved = !!key(target) && target.getAttribute("data-record-version") !== source.getAttribute("data-record-version");
    Array.prototype.slice.call(target.attributes).forEach(function (a) {
      if (!source.hasAttribute(a.name) && a.name !== "open") target.removeAttribute(a.name);
    });
    Array.prototype.slice.call(source.attributes).forEach(function (a) {
      if (a.name !== "open" && target.getAttribute(a.name) !== a.value) target.setAttribute(a.name, a.value);
    });
    reconcile(target, source, animate);
    if (target.tagName === "DETAILS") target.open = expanded;
    target.scrollTop = top; target.scrollLeft = left;
    if (moved && animate && target.offsetParent && !doc.hidden && !reduced() && target.animate) {
      target.animate([{ outline: "2px solid var(--accent)" }, { outline: "2px solid transparent" }],
                     { duration: 1100, easing: "ease-out" });
    }
    return target;
  }
  /* Make `target`'s children match `source`'s, reusing attached nodes by identity. `source` is a
     freshly built, detached element; its children are consumed. */
  function reconcile(target, source, animate) {
    var indexed = {}, used = new Set(), cursor = target.firstChild;
    Array.prototype.forEach.call(target.childNodes, function (n) { var k = key(n); if (k) indexed[k] = n; });
    Array.prototype.slice.call(source.childNodes).forEach(function (fresh) {
      var k = key(fresh), previous = k ? indexed[k] : cursor;
      if (used.has(previous) || !compatible(previous, fresh)) previous = null;
      var next = previous ? patch(previous, fresh, animate) : fresh;
      if (previous && previous === cursor) cursor = next;
      if (next !== cursor) target.insertBefore(next, cursor);
      used.add(next); cursor = next.nextSibling;
    });
    while (cursor) { var after = cursor.nextSibling; target.removeChild(cursor); cursor = after; }
  }

  global.HubSurface = { value: value, inline: inline, label: label, fields: fields,
                        checkpoints: checkpoints, reconcile: reconcile, button: button };
})(typeof window !== "undefined" ? window : this);
