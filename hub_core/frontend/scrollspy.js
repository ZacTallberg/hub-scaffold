/*
 * scrollspy.js — hub shared frontend kit
 * ---------------------------------------------------------------------------
 * Vanilla, dependency-free scroll-spy + deep-link handler for every project
 * hub. NO build step, NO framework, NO web fonts, NO external deps. Plain
 * ES2020+. See the hub doctrine sections 8 (Human View), 10 (a11y/perf/
 * mobile/print), 11 (Shared Assets).
 *
 * RESPONSIBILITIES
 *   1. Scroll-spy: an IntersectionObserver (NOT a scroll listener — async,
 *      off the main thread, no throttling needed) highlights the active
 *      `.hub-nav a` link as the matching `<section id="sec-...">` enters a
 *      reading band just under the sticky nav, and mirrors that section into
 *      `location.hash` via history.replaceState() so URL updates never cause
 *      scroll jank and never spam the back button.
 *   2. Deep-link handling: on initial load AND on every `hashchange`, find the
 *      element addressed by the hash, auto-expand every ancestor `<details>`,
 *      scroll it into view respecting `scroll-margin-top` (the sticky-nav
 *      offset), and flash-highlight it (animation gated behind
 *      prefers-reduced-motion). Works for nav anchors (#sec-*) AND per-row
 *      deep-link anchors (#task-0147, #adr-0017, ...).
 *
 * CSS CONTRACT (must be provided by tokens.css / the page; see notes at the
 * bottom of this file for the exact rules, including the `:target` no-JS
 * fallback the doctrine requires):
 *   --nav-h            sticky nav height; sections set scroll-margin-top:var(--nav-h)
 *   --ring             focus ring color (also reused for the flash outline)
 *   .hub-nav a         the nav anchor links this script toggles
 *   [aria-current="location"]   set on the active nav link (style it in CSS)
 *   .is-hub-flash      class this script adds for the flash highlight (~1.4s)
 *
 * PUBLIC API (so other kit files / hub HTML can match it):
 *   window.HubScrollSpy.init(options?) -> controller   (auto-runs on DOMContentLoaded)
 *   window.HubScrollSpy.refresh()                       (re-scan after DOM mutation, e.g. SSE swap)
 *   window.HubScrollSpy.goTo(idOrHash, opts?)           (programmatic deep-link)
 *   window.HubScrollSpy.destroy()                       (teardown: observers + listeners)
 *
 * Idempotent: calling init() twice destroys the prior instance first.
 * ---------------------------------------------------------------------------
 */
(function (global) {
  "use strict";

  /** Default configuration. Override via HubScrollSpy.init({...}). */
  var DEFAULTS = {
    navSelector: "nav.hub-nav",          // the sticky nav container
    linkSelector: "nav.hub-nav a[href^='#']", // anchor links inside it
    sectionSelector: "section[id^='sec-']",   // spied sections
    activeAttr: "aria-current",          // attribute set on the active link
    activeValue: "location",             // WCAG-friendly value for nav location
    activeClass: "is-active",            // class mirror for easy styling
    flashClass: "is-hub-flash",          // class added during the flash
    flashMs: 1400,                       // flash duration (must match CSS keyframes)
    // Fraction of the *post-nav* viewport used as the reading band. The
    // active section is the topmost one whose top has crossed below the nav
    // but is still within `bandFraction` of the remaining viewport.
    bandFraction: 0.35,
    historyMode: "replace",             // "replace" (no jank, no history spam) | "off"
    scrollContainer: null                // null => the document scrolling element
  };

  // ---- module state (single live instance) --------------------------------
  var state = null;

  // ---- small utils --------------------------------------------------------

  function isReducedMotion() {
    try {
      return global.matchMedia &&
        global.matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch (e) { return false; }
  }

  /** Read the sticky-nav height in px: prefer the live nav box, fall back to
   *  the --nav-h custom property, then a sane default. */
  function navHeightPx(cfg) {
    var nav = document.querySelector(cfg.navSelector);
    if (nav) {
      var r = nav.getBoundingClientRect();
      if (r.height > 0) return Math.round(r.height);
    }
    var raw = getComputedStyle(document.documentElement)
      .getPropertyValue("--nav-h").trim();
    var px = cssLengthToPx(raw);
    return px != null ? px : 64;
  }

  /** Convert a CSS length token (px / rem / em) to pixels; null if unknown. */
  function cssLengthToPx(value) {
    if (!value) return null;
    var m = /^(-?[\d.]+)(px|rem|em)?$/.exec(value);
    if (!m) return null;
    var n = parseFloat(m[1]);
    if (isNaN(n)) return null;
    var unit = m[2] || "px";
    if (unit === "px") return n;
    var root = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
    return Math.round(n * root); // rem and em both resolve against root here (good enough for nav-h)
  }

  /** Decode a hash to a raw element id (handles %xx and the leading '#'). */
  function idFromHash(hash) {
    if (!hash) return "";
    var h = hash.charAt(0) === "#" ? hash.slice(1) : hash;
    try { h = decodeURIComponent(h); } catch (e) { /* leave as-is */ }
    return h;
  }

  /** Safe element lookup by raw id (avoids CSS.escape pitfalls). */
  function byId(id) {
    if (!id) return null;
    return document.getElementById(id);
  }

  // ---- nav highlight ------------------------------------------------------

  /** Build a map: section element -> its nav <a>, and id -> nav <a>. */
  function buildLinkIndex(cfg) {
    var links = Array.prototype.slice.call(document.querySelectorAll(cfg.linkSelector));
    var byTargetId = Object.create(null);
    links.forEach(function (a) {
      var id = idFromHash(a.getAttribute("href") || "");
      if (id) byTargetId[id] = a;
    });
    return { links: links, byTargetId: byTargetId };
  }

  function setActiveLink(cfg, idx, sectionId) {
    var active = sectionId ? idx.byTargetId[sectionId] : null;
    idx.links.forEach(function (a) {
      if (a === active) {
        if (a.getAttribute(cfg.activeAttr) !== cfg.activeValue) {
          a.setAttribute(cfg.activeAttr, cfg.activeValue);
        }
        a.classList.add(cfg.activeClass);
      } else {
        if (a.hasAttribute(cfg.activeAttr)) a.removeAttribute(cfg.activeAttr);
        a.classList.remove(cfg.activeClass);
      }
    });
  }

  /** Mirror the active section into location.hash without triggering a scroll.
   *  history.replaceState keeps the back button clean (one entry for the page)
   *  while scrolling; deep-links the user explicitly clicks still pushState
   *  naturally via the anchor. */
  function syncHash(cfg, sectionId) {
    if (cfg.historyMode === "off") return;
    if (!sectionId) return;
    var want = "#" + sectionId;
    if (global.location.hash === want) return;
    try {
      global.history.replaceState(global.history.state, "", want);
    } catch (e) {
      // Some sandboxed contexts forbid history mutation; degrade silently —
      // the visible nav highlight still works.
    }
  }

  // ---- deep-link: expand <details>, scroll, flash -------------------------

  /** Expand every ancestor <details> of `el` so a deep-linked row is visible.
   *  Returns true if any was newly opened (caller may want to re-measure). */
  function expandAncestorDetails(el) {
    var changed = false;
    var node = el;
    while (node && node !== document.body) {
      if (node.tagName === "DETAILS" && !node.open) {
        node.open = true;
        changed = true;
      }
      node = node.parentElement;
    }
    // Also: if the element itself is (or contains) a closed <details>, open it.
    if (el && el.tagName === "DETAILS" && !el.open) { el.open = true; changed = true; }
    return changed;
  }

  /** Scroll an element into view respecting scroll-margin-top.
   *  Native scrollIntoView honors scroll-margin-top AND the html
   *  scroll-behavior preference, so it stays smooth/instant per the user's
   *  motion preference automatically. We add a manual offset fallback for the
   *  rare engine that ignores scroll-margin-top on programmatic scrolls. */
  function scrollToEl(cfg, el) {
    if (!el) return;
    var smooth = !isReducedMotion();
    var behavior = smooth ? "smooth" : "auto";
    try {
      el.scrollIntoView({ behavior: behavior, block: "start", inline: "nearest" });
    } catch (e) {
      el.scrollIntoView(); // ancient signature
    }
    // Defensive offset correction: if scroll-margin-top was ignored, the
    // element top will sit under the sticky nav. Nudge by the nav height.
    // (No-op on compliant engines because the element is already offset.)
    requestAnimationFrame(function () {
      var navH = navHeightPx(cfg);
      var top = el.getBoundingClientRect().top;
      if (top < navH - 2) {
        var dy = top - navH;
        var scroller = cfg.scrollContainer || global;
        try {
          scroller.scrollBy({ top: dy, left: 0, behavior: behavior });
        } catch (e2) {
          global.scrollBy(0, dy);
        }
      }
    });
  }

  /** Flash-highlight a deep-linked element. Adds .is-hub-flash for flashMs,
   *  then removes it. Under prefers-reduced-motion we still apply the class
   *  briefly (CSS gates the *animation*, not a static highlight, so the user
   *  gets a non-animated cue). Re-triggers cleanly on repeat navigation. */
  function flash(cfg, el) {
    if (!el) return;
    el.classList.remove(cfg.flashClass);
    // force reflow so re-adding restarts the CSS animation
    // eslint-disable-next-line no-unused-expressions
    void el.offsetWidth;
    el.classList.add(cfg.flashClass);
    if (el.__hubFlashTimer) clearTimeout(el.__hubFlashTimer);
    el.__hubFlashTimer = setTimeout(function () {
      el.classList.remove(cfg.flashClass);
      el.__hubFlashTimer = null;
    }, cfg.flashMs);
  }

  /** Move focus to the deep-linked element for keyboard/AT users without
   *  stealing scroll (focus can itself scroll, so we guard with preventScroll
   *  and add a transient tabindex on non-focusable targets). 4.1.2 name/role. */
  function focusTarget(el) {
    if (!el) return;
    var focusable = el.matches(
      "a[href],button,input,select,textarea,[tabindex],summary,details"
    );
    var addedTab = false;
    if (!focusable) {
      el.setAttribute("tabindex", "-1");
      addedTab = true;
    }
    try { el.focus({ preventScroll: true }); }
    catch (e) { try { el.focus(); } catch (e2) {} }
    if (addedTab) {
      // remove the synthetic tabindex once focus leaves so the DOM stays clean
      el.addEventListener("blur", function handler() {
        el.removeAttribute("tabindex");
        el.removeEventListener("blur", handler);
      });
    }
  }

  /** The full deep-link routine for a given raw id. */
  function activateTarget(cfg, id, opts) {
    opts = opts || {};
    var el = byId(id);
    if (!el) return false;
    expandAncestorDetails(el);
    // Let the just-opened <details> reflow before measuring/scrolling.
    requestAnimationFrame(function () {
      scrollToEl(cfg, el);
      if (opts.flash !== false) flash(cfg, el);
      if (opts.focus !== false) focusTarget(el);
    });
    return true;
  }

  // ---- IntersectionObserver scroll-spy ------------------------------------

  function makeObserver(cfg, idx) {
    var navH = navHeightPx(cfg);
    var vh = global.innerHeight ||
      document.documentElement.clientHeight || 800;
    // Reading band: from just below the nav down to bandFraction of the
    // remaining viewport. Top margin = -navH (nav-occluded zone is ignored,
    // satisfying WCAG 2.4.11 Focus-Not-Obscured by construction). Bottom
    // margin pushes the active threshold up so the section becomes "active"
    // when its top reaches the upper third — matching reading position.
    var bottom = Math.max(0, Math.round(vh * (1 - cfg.bandFraction)));
    var rootMargin = (-navH) + "px 0px " + (-bottom) + "px 0px";

    var sections = Array.prototype.slice.call(
      document.querySelectorAll(cfg.sectionSelector)
    );

    // Track which sections currently intersect the band; pick the topmost.
    var visible = new Set();

    function recompute() {
      if (visible.size === 0) {
        // Nothing in the band. Two edge cases:
        //  - scrolled above the first section => clear highlight
        //  - scrolled to the very bottom (last section shorter than band) =>
        //    keep the last section active so the nav never goes blank mid-doc.
        var sel = document.scrollingElement || document.documentElement;
        var atBottom = (sel.scrollTop + sel.clientHeight) >=
          (sel.scrollHeight - 2);
        if (atBottom && sections.length) {
          var last = sections[sections.length - 1];
          setActiveLink(cfg, idx, last.id);
          syncHash(cfg, last.id);
        } else {
          setActiveLink(cfg, idx, null);
        }
        return;
      }
      // Topmost visible section (smallest offsetTop) wins.
      var best = null, bestTop = Infinity;
      visible.forEach(function (sec) {
        var t = sec.getBoundingClientRect().top;
        if (t < bestTop) { bestTop = t; best = sec; }
      });
      if (best) {
        setActiveLink(cfg, idx, best.id);
        syncHash(cfg, best.id);
      }
    }

    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) visible.add(en.target);
        else visible.delete(en.target);
      });
      recompute();
    }, { root: cfg.scrollContainer || null, rootMargin: rootMargin, threshold: 0 });

    sections.forEach(function (s) { io.observe(s); });
    return { io: io, sections: sections, recompute: recompute };
  }

  // ---- click handling: smooth-scroll same-page nav links via our offset ---

  function onNavClick(cfg, e) {
    // Let modified clicks / new-tab behave normally.
    if (e.defaultPrevented || e.button !== 0 || e.metaKey ||
        e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest && e.target.closest(cfg.linkSelector);
    if (!a) return;
    var id = idFromHash(a.getAttribute("href") || "");
    var el = byId(id);
    if (!el) return; // unknown target => let the browser try
    e.preventDefault();
    // Push a real history entry for an explicit click (back button returns
    // the user to where they were), then run the unified deep-link routine.
    var want = "#" + id;
    if (cfg.historyMode !== "off" && global.location.hash !== want) {
      try { global.history.pushState(global.history.state, "", want); }
      catch (err) { global.location.hash = want; }
    }
    activateTarget(cfg, id, { flash: id.indexOf("sec-") !== 0, focus: true });
  }

  // ---- lifecycle ----------------------------------------------------------

  function init(options) {
    if (state) destroy();
    var cfg = Object.assign({}, DEFAULTS, options || {});
    var idx = buildLinkIndex(cfg);
    var obs = makeObserver(cfg, idx);

    // Delegated click handler for in-page nav links (and any deep-link anchor).
    var clickHandler = function (e) { onNavClick(cfg, e); };
    document.addEventListener("click", clickHandler, false);

    // Deep-link on hashchange (back/forward, palette jumps, external links).
    var hashHandler = function () {
      var id = idFromHash(global.location.hash);
      if (id) activateTarget(cfg, id, { flash: true, focus: true });
    };
    global.addEventListener("hashchange", hashHandler, false);

    // Re-tune rootMargin on resize / orientation change (nav height & viewport
    // both shift). Debounced via rAF.
    var resizePending = false;
    var resizeHandler = function () {
      if (resizePending) return;
      resizePending = true;
      requestAnimationFrame(function () {
        resizePending = false;
        rebuildObserver();
      });
    };
    global.addEventListener("resize", resizeHandler, false);
    global.addEventListener("orientationchange", resizeHandler, false);

    function rebuildObserver() {
      if (state && state.obs && state.obs.io) state.obs.io.disconnect();
      idx = buildLinkIndex(cfg);
      obs = makeObserver(cfg, idx);
      if (state) { state.idx = idx; state.obs = obs; }
    }

    state = {
      cfg: cfg, idx: idx, obs: obs,
      clickHandler: clickHandler,
      hashHandler: hashHandler,
      resizeHandler: resizeHandler,
      rebuildObserver: rebuildObserver
    };

    // Initial deep-link: if the page loaded with a hash, honor it AFTER layout
    // settles (the browser's native jump can land under the sticky nav and
    // before <details> are expanded). One frame is enough post-DOMContentLoaded;
    // we also wait for fonts/images-agnostic layout via a double rAF.
    var initialId = idFromHash(global.location.hash);
    if (initialId) {
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          activateTarget(cfg, initialId, { flash: true, focus: true });
        });
      });
    }

    return publicController();
  }

  function refresh() {
    if (!state) return init();
    state.rebuildObserver();
    state.obs.recompute();
    return publicController();
  }

  function goTo(idOrHash, opts) {
    if (!state) init();
    var id = idFromHash(String(idOrHash));
    if (!id) return false;
    var want = "#" + id;
    if (state.cfg.historyMode !== "off" && global.location.hash !== want) {
      try { global.history.pushState(global.history.state, "", want); }
      catch (e) { /* ignore */ }
    }
    return activateTarget(state.cfg, id, opts || { flash: true, focus: true });
  }

  function destroy() {
    if (!state) return;
    if (state.obs && state.obs.io) state.obs.io.disconnect();
    document.removeEventListener("click", state.clickHandler, false);
    global.removeEventListener("hashchange", state.hashHandler, false);
    global.removeEventListener("resize", state.resizeHandler, false);
    global.removeEventListener("orientationchange", state.resizeHandler, false);
    // Clear any active highlight/flash classes.
    if (state.idx) {
      state.idx.links.forEach(function (a) {
        a.removeAttribute(state.cfg.activeAttr);
        a.classList.remove(state.cfg.activeClass);
      });
    }
    state = null;
  }

  function publicController() {
    return { init: init, refresh: refresh, goTo: goTo, destroy: destroy };
  }

  // ---- export + auto-init -------------------------------------------------

  global.HubScrollSpy = { init: init, refresh: refresh, goTo: goTo, destroy: destroy };

  function autoInit() {
    // Respect a manual opt-out: <body data-hub-scrollspy="off">
    var body = document.body;
    if (body && body.getAttribute("data-hub-scrollspy") === "off") return;
    init();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", autoInit, { once: true });
  } else {
    autoInit();
  }

})(typeof window !== "undefined" ? window : this);

/* ===========================================================================
 * CSS CONTRACT — add these rules to tokens.css (or the page <style>). They are
 * documented here so the kit stays cohesive and so the deep-link behavior
 * degrades gracefully with JavaScript OFF (the doctrine's `:target` no-JS
 * fallback requirement, section 8 + 10).
 *
 *   :root { --nav-h: 3.5rem; }   (* the single source of truth for nav height *)
 *
 *   (* Sections sit clear of the sticky nav on any scroll, native or scripted *)
 *   (* — WCAG 2.4.11 Focus-Not-Obscured. scrollIntoView + :target both honor  *)
 *   (* scroll-margin-top automatically.                                        *)
 *   :where(section[id^="sec-"], [id^="task-"], [id^="adr-"], [id^="feat-"],
 *          [id^="gap-"],  [id^="cap-"],  [id^="deploy-"]) {
 *     scroll-margin-top: calc(var(--nav-h) + 0.5rem);
 *   }
 *
 *   (* Smooth scroll ONLY when the user has not asked for reduced motion.      *)
 *   @media (prefers-reduced-motion: no-preference) {
 *     html { scroll-behavior: smooth; }
 *   }
 *
 *   (* Active nav link — script sets aria-current="location" + .is-active.     *)
 *   (* Use a non-color cue too (weight/underline) so it is not color-alone.    *)
 *   .hub-nav a[aria-current="location"], .hub-nav a.is-active {
 *     color: var(--accent);
 *     font-weight: 600;
 *     box-shadow: inset 0 -2px 0 0 var(--accent);
 *   }
 *
 *   (* Focus ring — single --ring token, 2px, >=3:1 (WCAG 2.4.13).            *)
 *   :focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
 *
 *   (* ---- DEEP-LINK FLASH ------------------------------------------------- *)
 *   (* JS path: .is-hub-flash animates a fading highlight (~1.4s, matches      *)
 *   (* flashMs). prefers-reduced-motion swaps the animation for a static,     *)
 *   (* non-animated highlight that the script clears after flashMs.           *)
 *   @keyframes hub-flash {
 *     from { background: var(--info-bg, color-mix(in oklch, var(--accent) 22%, transparent)); }
 *     to   { background: transparent; }
 *   }
 *   .is-hub-flash {
 *     animation: hub-flash 1.4s ease-out;
 *     border-radius: var(--radius, 6px);
 *   }
 *   @media (prefers-reduced-motion: reduce) {
 *     .is-hub-flash {
 *       animation: none;
 *       background: color-mix(in oklch, var(--accent) 18%, transparent);
 *       outline: 2px solid var(--ring);
 *       outline-offset: 1px;
 *     }
 *   }
 *
 *   (* ---- :target NO-JS FALLBACK ----------------------------------------- *)
 *   (* With JS disabled, the browser still jumps to #id and applies :target.  *)
 *   (* This gives the same persistent highlight (no fade — pure CSS cannot     *)
 *   (* time-out a state cleanly, and a persistent cue is the accessible        *)
 *   (* choice). Also reveal a deep-linked row inside a closed <details> via    *)
 *   (* the CSS-only `details:has(:target)` open hint where supported.          *)
 *   :target {
 *     background: color-mix(in oklch, var(--accent) 16%, transparent);
 *     border-radius: var(--radius, 6px);
 *     scroll-margin-top: calc(var(--nav-h) + 0.5rem);
 *   }
 *   (* Best-effort no-JS expand: force the <details> containing the target     *)
 *   (* open. `details:has(:target)` is widely supported in 2026 baseline.      *)
 *   details:has(:target) { }            (* placeholder: see note below *)
 *   (* NOTE: <details> cannot be force-opened by CSS alone (the `open`        *)
 *   (* attribute is not a CSS property). The robust no-JS reveal is to make    *)
 *   (* the hidden content reachable when targeted: author rows so the          *)
 *   (* deep-linked content lives in a sibling that `:target`/`:has` can show,  *)
 *   (* OR accept that JS-off users see the closed summary and the highlight     *)
 *   (* lands on the <summary>. With JS ON (the common path) expandAncestor-     *)
 *   (* Details() opens it. This is the documented graceful-degradation seam.   *)
 * =========================================================================== */
