/* Header takeover -- the shared banner knows about the header it landed on top of.

   An app adopting the shared banner (#app-banner) usually already HAS a header, and a banner
   that cannot tell "I replaced the header" from "I landed on top of it" looks exactly like a
   success while the page grows a second header row and two of everything. This companion runs
   after the banner has drawn (`app-banner:ready`) and does four things, only one of which ever
   hides anything the app did not name:

     1. data-replaces="<selector[, selector]>" on #app-banner: the APP names the row the banner
        stands in for. Those elements are HIDDEN (data-ab-replaced), never removed -- apps bind
        their own controls by id at startup, often with no null guard, and a deleted element
        throws and takes the rest of that app's boot with it. The reachable controls that went
        dark with it are named in a warning, so the day somebody adds a live button to a header
        that is hidden, one line says so. data-replaces="" is the explicit "nothing to replace".
     2. THE DETECTOR, when data-replaces is ABSENT: a visible header outside the banner holding
        reachable controls is WARNED about (element, control count, the attribute to declare).
        It never hides: a header can carry the button that stops a running job, and a guess that
        took it away would do so silently on an app whose author asked for none of this.
     3. data-ab-superseded on any app element (or a wrapper) says "the banner already offers
        this": it is hidden ONCE the banner has really drawn -- from here, never from CSS, so a
        banner that never loads leaves the app's own copy in place -- and it stops counting as
        a loss in (1).
     4. DUPLICATES IN THE BANNER'S SLOT. An app control moved into [data-ab-slot] that does
        what a control the banner is drawing RIGHT NOW does (settings, your apps, sign out, the
        tour, the theme, search) is hidden, checked per control and asked twice (at ready, and
        again after the banner's late panels arrive). A control whose banner counterpart is not
        drawn is left alone -- hiding the app's search box when the standard search bar is not
        mounted would leave that app with no way to search. data-ab-keep="<why>" keeps one and
        prints the reason, because the next reader of that markup must know which case it is.

     5. ONE OF EACH IN THE DRAWER. The banner draws its own Guided tour, About this app (the
        read-me) and, for admins, People and Permissions; an app that ALSO lists one of those in its
        `profile` / `admin` / `help` island gets two rows that do one thing. The app's copy is hidden
        while the banner's own row is drawn (the banner's rows are the buttons without an id or
        href; an app's island rows carry one or the other), and an app row repeated with no banner
        counterpart keeps its first copy. Each drop is reported once, naming the row. The drawer is
        rebuilt every time it opens, so this runs on every rebuild.

   And one removal that is a PERSON's, never a rule: the `hide_custom` component property (one
   control name or #id per line) takes an app's own control off the band for everyone on that app,
   reversibly, scoped to the slot and nowhere else.

   Every outcome is reported once: console.warn/info, and `app-banner:duplicate` on document
   (detail {kind, control, action, reason}) so an app's error visibility can carry it.

     <script src="/hub/components/header-takeover/header-takeover.js" defer></script>   (after banner.js)

   Standard browser APIs only; no framework, no build step. */
(function () {
  "use strict";
  var VERSION = "1.0.0";
  var doc = document, win = window;
  var LOG = "[header-takeover] ";
  var reported = {};
  var hideCustom = [];

  function banner() { return doc.getElementById("app-banner"); }

  function report(kind, control, action, reason, level) {
    var key = kind + "|" + control + "|" + action;
    if (reported[key]) return;
    reported[key] = true;
    (level === "info" ? console.info : console.warn)(LOG + reason);
    try {
      doc.dispatchEvent(new CustomEvent("app-banner:duplicate",
        { detail: { kind: kind, control: control, action: action, reason: reason } }));
    } catch (e) {}
  }

  function visible(node) {
    if (!node || !node.getClientRects().length) return false;
    var cs = win.getComputedStyle(node);
    return cs.visibility !== "hidden" && cs.display !== "none";
  }

  function hide(node, attr) {
    if (node.getAttribute(attr) === "1") return;
    node.setAttribute(attr, "1");
    node.setAttribute("data-ab-prev-display", node.style.display || "");
    node.style.display = "none";
  }

  function unhide(node, attr) {
    if (node.getAttribute(attr) !== "1") return;
    node.removeAttribute(attr);
    node.style.display = node.getAttribute("data-ab-prev-display") || "";
    node.removeAttribute("data-ab-prev-display");
  }

  function describe(node) {
    if (node.id) return "#" + node.id;
    var name = nameOf(node);
    return node.tagName.toLowerCase() + (node.className && typeof node.className === "string"
      ? "." + node.className.trim().split(/\s+/)[0] : "") + (name ? " \"" + name.slice(0, 40) + "\"" : "");
  }

  function nameOf(node) {
    return String(node.getAttribute("aria-label") || node.getAttribute("data-ab-tip") ||
                  node.getAttribute("title") || node.getAttribute("placeholder") ||
                  node.textContent || "").replace(/\s+/g, " ").trim();
  }

  var CONTROLS = "button, a[href], input:not([type=hidden]), select, textarea, [role=button], [tabindex]:not([tabindex='-1'])";

  function kept(node) { return node.closest("[data-ab-keep]"); }
  function superseded(node) { return node.closest("[data-ab-superseded]"); }

  /* WHAT A CONTROL DOES, read from its name. The banner's own controls are read the same way, so a
     role is "drawn by the banner" only when a banner element with that role is in the DOM now. */
  var ROLES = [
    ["settings", /^(your )?(settings|account|profile|preferences)\b/],
    ["apps", /^(your )?apps\b|app switcher/],
    ["signout", /^(sign|log) ?out\b/],
    ["tour", /guided tour|^tour$|take the tour/],
    ["about", /about this app|read ?me/],
    ["theme", /theme|dark mode|light mode|switch to (dark|light)/],
    ["people", /people and permissions/],
    ["search", /^search\b|\bsearch$/]
  ];
  function roleOf(node) {
    var n = nameOf(node).toLowerCase();
    if (node.matches && node.matches("input[type=search], [role=search], form[role=search]")) return "search";
    for (var i = 0; i < ROLES.length; i++) if (ROLES[i][1].test(n)) return ROLES[i][0];
    return "";
  }

  function bandRoles(mount, slot) {
    // The band and its own surfaces (drawer, popovers), excluding the app's slot.
    var nodes = [].slice.call(mount.querySelectorAll(CONTROLS))
      .concat([].slice.call(doc.querySelectorAll(".ab-surface " + CONTROLS.split(", ").join(", .ab-surface "))));
    var roles = {};
    nodes.forEach(function (n) {
      if (slot && slot.contains(n)) return;
      var r = roleOf(n);
      if (r) roles[r] = true;
    });
    var search = doc.getElementById("app-search") || doc.querySelector("[data-app-search]");
    if (search && search.getAttribute("data-sb-ready") === "1") roles.search = true;
    return roles;
  }

  function reachable(root) {
    return [].slice.call(root.querySelectorAll(CONTROLS)).filter(function (c) {
      return visible(c) && !superseded(c) && !kept(c);
    });
  }

  /* ---------------------------------------------------------------- the pass */

  function pass() {
    var mount = banner();
    if (!mount || !mount.getAttribute("data-ab-ready")) return;
    var slot = mount.querySelector("[data-ab-slot]");

    // (1) the header the app named, and (2) the detector when it named nothing.
    var declared = mount.getAttribute("data-replaces");
    if (declared) {
      declared.split(",").forEach(function (sel) {
        sel = sel.trim();
        if (!sel) return;
        var found;
        try { found = doc.querySelectorAll(sel); } catch (e) {
          report("replaces", sel, "invalid", "data-replaces selector \"" + sel + "\" is not valid CSS and was ignored.");
          return;
        }
        if (!found.length) {
          report("replaces", sel, "missing", "data-replaces=\"" + sel + "\" matched nothing on this page.", "info");
        }
        [].forEach.call(found, function (node) {
          if (node === mount || node.contains(mount) || mount.contains(node)) {
            report("replaces", sel, "refused", "data-replaces=\"" + sel + "\" matches an element holding the banner; it was NOT hidden.");
            return;
          }
          var lost = reachable(node).map(describe);   // measured BEFORE hiding: hidden things are not visible
          hide(node, "data-ab-replaced");
          if (lost.length) {
            report("replaces", sel, "dark", "Hid " + describe(node) + " as data-replaces asked; " + lost.length +
                   " reachable control(s) went with it: " + lost.join(", ") +
                   ". Mark each the banner already offers with data-ab-superseded, or move it into [data-ab-slot].");
          }
        });
      });
    } else if (declared === null) {
      [].forEach.call(doc.querySelectorAll("header, [role=banner]"), function (node) {
        if (node === mount || node.contains(mount) || mount.contains(node) || !visible(node)) return;
        if (node.getBoundingClientRect().top > 240) return;          // a header near the top only
        var controls = reachable(node);
        if (!controls.length) return;
        report("detector", describe(node), "warn", "The banner sits above another header, " + describe(node) +
               ", with " + controls.length + " reachable control(s). Nothing was hidden. If the banner replaces it, " +
               "declare data-replaces=\"<selector>\" on #app-banner; if both belong, declare data-replaces=\"\".");
      });
    }

    // (3) app elements that say the banner already offers them.
    [].forEach.call(doc.querySelectorAll("[data-ab-superseded]"), function (node) {
      if (mount.contains(node)) return;
      hide(node, "data-ab-superseded-hidden");
    });

    if (!slot) return;
    var roles = bandRoles(mount, slot);
    // (4) duplicates in the slot, per control, only while the banner draws the counterpart.
    [].forEach.call(slot.querySelectorAll(CONTROLS), function (node) {
      if (node.closest("#app-search, [data-app-search]")) return;          // the standard search is not a duplicate
      var role = roleOf(node);
      if (!role) return;
      var target = node.closest("[data-ab-keep]") || node;
      var keep = kept(node);
      if (keep) {
        report("duplicate", describe(node), "kept", "Kept " + describe(node) + " beside the banner's own " + role +
               ", as the app asked: " + keep.getAttribute("data-ab-keep"), "info");
        return;
      }
      if (roles[role]) {
        hide(target, "data-ab-duplicate");
        report("duplicate", describe(node), "hidden", "Hid " + describe(node) + ": the banner already draws " + role +
               " here. Add data-ab-keep=\"<why>\" to keep it.", "info");
      } else if (role === "search") {
        report("duplicate", describe(node), "adopt", describe(node) + " is this app's own search box; it was kept " +
               "because the standard search bar is not mounted. Adopt /hub/components/search-bar/.");
      }
    });

    // The person's removal: hide_custom, reversible on every pass, scoped to the slot.
    var want = hideCustom.map(function (s) { return String(s).trim().toLowerCase(); }).filter(Boolean);
    [].forEach.call(slot.querySelectorAll("[data-ab-offband]"), function (n) { unhide(n, "data-ab-offband"); });
    if (want.length) {
      [].forEach.call(slot.querySelectorAll(CONTROLS), function (node) {
        if (node.closest("#app-search, [data-app-search]")) return;
        var name = nameOf(node).toLowerCase(), id = node.id ? "#" + node.id.toLowerCase() : "";
        if (want.indexOf(name) >= 0 || (id && want.indexOf(id) >= 0)) {
          hide(node, "data-ab-offband");
          report("hide_custom", describe(node), "hidden", "Took " + describe(node) +
                 " off the banner: an operator listed it in hide_custom.", "info");
        }
      });
    }
  }

  /* ------------------------------------------------ (5) one of each in the drawer */

  var DRAWER_ROLES = { tour: true, about: true, people: true };

  function dedupeDrawer(drawer) {
    var rows = [].slice.call(drawer.querySelectorAll(".ab-row"));
    var owned = {}, seen = {};
    rows.forEach(function (row) {
      var role = roleOf(row);
      // The banner's own rows are buttons with neither an id nor an href; island rows carry one.
      if (DRAWER_ROLES[role] && row.tagName === "BUTTON" && !row.id && !row.closest("form")) owned[role] = row;
    });
    rows.forEach(function (row) {
      var role = roleOf(row);
      if (!DRAWER_ROLES[role] || owned[role] === row) return;
      var label = nameOf(row).slice(0, 60);
      if (row.closest("[data-ab-keep]")) return;
      if (owned[role]) {
        hide(row, "data-ab-duplicate");
        report("sidebar", label, "hidden", "Hid the app's drawer row \"" + label + "\": the banner already draws " +
               role + " there.", "info");
      } else if (seen[role]) {
        hide(row, "data-ab-duplicate");
        report("sidebar", label, "hidden", "Hid a second drawer row for " + role + " (\"" + label +
               "\"); the first one is kept.", "info");
      } else {
        seen[role] = row;
      }
    });
    // A section left with nothing visible loses its heading too.
    [].forEach.call(drawer.querySelectorAll(".ab-sec"), function (sec) {
      var live = [].some.call(sec.querySelectorAll(".ab-row"), function (r) { return r.getAttribute("data-ab-duplicate") !== "1"; });
      if (live) unhide(sec, "data-ab-duplicate"); else hide(sec, "data-ab-duplicate");
    });
  }

  function watchDrawer() {
    var drawer = doc.querySelector(".ab-drawer");
    if (!drawer || drawer.getAttribute("data-ht-watched")) return;
    drawer.setAttribute("data-ht-watched", "1");
    var busy = false;
    var obs = new MutationObserver(function () {
      if (busy) return;
      busy = true;
      try { dedupeDrawer(drawer); } finally { busy = false; obs.takeRecords(); }
    });
    obs.observe(drawer, { childList: true, subtree: true });
    dedupeDrawer(drawer);
  }

  /* ------------------------------------------------------------- properties */

  function takeProps(all) {
    var mine = all && (all["header-takeover"] || (all.props && all.props["header-takeover"]));
    var list = mine && mine.hide_custom;
    hideCustom = Array.isArray(list) ? list : (typeof list === "string" ? list.split(/\n/) : []);
  }
  try { if (win.HubComponentProps) takeProps(win.HubComponentProps); } catch (e) {}
  win.addEventListener("hub:component-props", function (e) {
    var det = (e && e.detail) || {};
    var mount = banner();
    if (det.app && mount && det.app !== mount.getAttribute("data-app")) return;
    takeProps(det.props || det);
    pass();
  });

  function start() {
    var mount = banner();
    if (!mount) return;
    var props = mount.getAttribute("data-props");
    if (props) {
      fetch(props + (props.indexOf("?") === -1 ? "?" : "&") + "component=header-takeover",
            { credentials: "same-origin", headers: { "Accept": "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (b) { if (b && b.props) { takeProps(b.props); pass(); } })
        .catch(function () {});
    }
    pass();
    watchDrawer();
    // ASKED TWICE: panels that arrive over the wire after the first draw are only there later.
    setTimeout(function () { pass(); watchDrawer(); }, 1500);
    doc.documentElement.setAttribute("data-header-takeover", VERSION);
  }

  var started = false;
  function startOnce() { if (!started && banner() && banner().getAttribute("data-ab-ready")) { started = true; start(); } }
  doc.addEventListener("app-banner:ready", startOnce);
  startOnce();
  // Loaded before the banner's mount existed (an async or head script): try again once the
  // document is parsed, and once more after load, before concluding there is no banner.
  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", startOnce, { once: true });
  win.addEventListener("load", startOnce, { once: true });
})();
