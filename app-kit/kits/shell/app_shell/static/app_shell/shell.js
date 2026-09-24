/* The app shell — one implementation of every capability, each ARMED only when the app declares
 * it (settings.APP_SHELL, rendered into #app-shell-caps). A capability not declared binds no
 * listener, renders no markup and issues no fetch.
 *
 * Always on, because they are not features but the difference between usable and merely
 * rendered: the theme toggle, keyboard scrolling of the one scroll region, and polls that pause
 * while the tab is hidden.
 *
 * No framework. Works with htmx when present (swaps, transitions, poll pausing) and without it.
 */
(function () {
  "use strict";

  const CAP = (function () {
    const node = document.getElementById("app-shell-caps");
    if (!node) return {};
    try { return JSON.parse(node.textContent) || {}; }
    catch (e) {
      /* A malformed island loses the declared capabilities, never the core. */
      console.warn("app shell: capability island is not valid JSON; capabilities off", e);
      return {};
    }
  })();

  const Shell = { cap: CAP };
  window.AppShell = Shell;

  /* ---------------------------------------------------------------- theme */
  function armTheme() {
    const root = document.documentElement;
    document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
        root.setAttribute("data-theme", next);
        /* Persistence can fail (private mode); the theme still changes for this page. */
        try { localStorage.setItem("app-theme", next); } catch (e) { /* not fatal */ }
      });
    });
  }

  /* ---------------------------------------------------- keyboard scrolling
     <body> is pinned so the header stays put, which takes the document's own scroll away. The
     browser only answers PageDown/Home/End on a region that holds focus, and a page opens with
     focus on <body> — so without this, PageDown moves 0px and the app is unusable without a
     mouse. Which element scrolls is resolved per keystroke: some pages put the scroller on
     <main>, others give each pane its own overflow region inside it. */
  function armKeyboardScrolling() {
    const main = document.getElementById("main");
    if (!main) return;
    function scrollable(el) {
      if (el.scrollHeight <= el.clientHeight) return false;
      const how = getComputedStyle(el).overflowY;
      return how === "auto" || how === "scroll";
    }
    function scroller() {
      if (scrollable(main)) return main;
      for (const el of main.querySelectorAll("*")) { if (scrollable(el)) return el; }
      return main;
    }
    document.addEventListener("keydown", function (event) {
      if (event.defaultPrevented || event.ctrlKey || event.altKey || event.metaKey) return;
      const t = event.target;
      if (t && t !== document.body && t !== main && t !== document.documentElement) return;
      const box = scroller();
      const page = Math.max(box.clientHeight - 60, 60);
      let by = 0;
      if (event.key === "Home" || event.key === "End") {
        box.scrollTo({ top: event.key === "Home" ? 0 : box.scrollHeight });
        event.preventDefault();
        return;
      }
      if (event.key === "PageDown") by = page;
      else if (event.key === "PageUp") by = -page;
      else if (event.key === "ArrowDown") by = 60;
      else if (event.key === "ArrowUp") by = -60;
      else if (event.key === " ") by = event.shiftKey ? -page : page;
      if (!by) return;
      box.scrollBy({ top: by });
      event.preventDefault();
    });
  }

  /* ---------------------------------------------------------- poll pausing
     Timer-driven htmx requests are cancelled while the tab is hidden; a click or submit is a
     real action and always goes through. On return, one immediate sync: elements opt in with
     hx-trigger="every 10s, visible-again from:body". */
  function armPollPause() {
    document.addEventListener("htmx:beforeRequest", function (event) {
      if (!document.hidden) return;
      const elt = event.detail && event.detail.elt;
      const trigger = (elt && elt.getAttribute && elt.getAttribute("hx-trigger")) || "";
      if (trigger.indexOf("every") !== -1) event.preventDefault();
    });
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) document.body.dispatchEvent(new CustomEvent("visible-again"));
    });
  }

  /* ---------------------------------------------------------------- toasts
     A status is carried by a word and a glyph as well as a colour. "Saved" collapses onto ONE
     row: a form that saves six times reassures six times in the same place. */
  const GLYPH = { ok: "✓", error: "!", warn: "!", info: "i" };
  function toast(message, kind, ms) {
    const stack = document.querySelector("[data-toasts]");
    if (!CAP.toasts || !stack) { console.info("toast:", message); return null; }
    kind = kind || "info";
    const row = document.createElement("div");
    row.className = "toast toast-" + kind;
    const glyph = document.createElement("span");
    glyph.className = "toast-glyph";
    glyph.setAttribute("aria-hidden", "true");
    glyph.textContent = GLYPH[kind] || "i";
    const text = document.createElement("span");
    text.className = "toast-text";
    text.textContent = (kind === "error" ? "Error: " : "") + message;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "toast-close";
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "×";
    close.addEventListener("click", function () { row.remove(); });
    row.append(glyph, text, close);
    stack.append(row);
    window.setTimeout(function () { row.remove(); }, ms || 4000);
    return row;
  }
  let savedRow = null, savedTimer = null;
  function flashSaved(message) {
    if (!CAP.toasts) return;
    if (savedRow && savedRow.isConnected) {
      savedRow.querySelector(".toast-text").textContent = message || "Saved";
    } else {
      savedRow = toast(message || "Saved", "ok", 60000);
    }
    window.clearTimeout(savedTimer);
    savedTimer = window.setTimeout(function () { if (savedRow) savedRow.remove(); savedRow = null; }, 2000);
  }
  Shell.toast = toast;
  Shell.flashSaved = flashSaved;
  window.addEventListener("app-toast", function (event) {
    const d = event.detail || {};
    toast(d.message || String(d), d.kind, d.ms);
  });

  /* ---------------------------------------------------------------- drawer
     A right-hand panel keeps the list visible, so you never lose your place. Opened by
     [data-drawer-url] or an `app-drawer` event {url, title}. */
  function armDrawer() {
    const drawer = document.querySelector("[data-drawer]");
    if (!drawer) return;
    const body = drawer.querySelector("[data-drawer-body]");
    const title = drawer.querySelector("[data-drawer-title]");
    let opener = null, generation = 0, controller = null;
    function close() {
      drawer.hidden = true;
      generation += 1;
      if (controller) controller.abort();
      if (opener && opener.focus) opener.focus();
    }
    async function open(url, label) {
      opener = document.activeElement;
      const mine = ++generation;
      if (controller) controller.abort();
      controller = new AbortController();
      drawer.hidden = false;
      title.textContent = label || "Detail";
      body.setAttribute("aria-busy", "true");
      body.innerHTML = '<p class="muted">Loading…</p>';
      let html;
      const warmed = Shell.takeWarm && Shell.takeWarm(url);
      try {
        html = warmed ? await warmed : await fetchPartial(url, controller.signal);
      } catch (error) {
        if (error.name === "AbortError") return;
        if (warmed) {
          /* A speculative request is only a hint: the deliberate click gets its own. */
          try { html = await fetchPartial(url, controller.signal); }
          catch (retry) { if (retry.name === "AbortError") return; }
        }
      }
      if (mine !== generation) return;           /* a later click already won */
      body.setAttribute("aria-busy", "false");
      if (html === undefined) {
        body.innerHTML = '<p class="muted">That detail could not be loaded. Nothing on the page ' +
          "has changed.</p>";
        return;
      }
      body.innerHTML = html;
      if (window.htmx) window.htmx.process(body);
      drawer.querySelector("[data-drawer-close]").focus();
    }
    Shell.openDrawer = open;
    Shell.closeDrawer = close;
    drawer.querySelector("[data-drawer-close]").addEventListener("click", close);
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !drawer.hidden) close(); });
    window.addEventListener("app-drawer", function (e) { if (e.detail && e.detail.url) open(e.detail.url, e.detail.title); });
    document.addEventListener("click", function (e) {
      const link = e.target.closest && e.target.closest("[data-drawer-url]");
      if (!link || e.ctrlKey || e.metaKey || e.shiftKey) return;
      e.preventDefault();
      open(link.getAttribute("data-drawer-url"), link.getAttribute("data-drawer-title") || link.textContent.trim());
    });
  }

  function fetchPartial(url, signal, prefetch) {
    const headers = { "HX-Request": "true", "X-Requested-With": "fetch" };
    if (prefetch) headers["X-Prefetch"] = "1";
    return fetch(url, { credentials: "same-origin", headers: headers, signal: signal })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.text(); });
  }
  Shell.fetchPartial = fetchPartial;

  /* --------------------------------------------------------------- confirm
     <form data-confirm="What this will do"> is held until the person agrees. Fails CLOSED:
     without <dialog> support the submit is held, never waved through. */
  function armConfirm() {
    const dlg = document.querySelector("[data-confirm-dialog]");
    const supported = dlg && typeof dlg.showModal === "function";
    let pending = null;
    function settle(ok) {
      const form = ok ? pending : null;
      pending = null;
      if (supported) { try { dlg.close(); } catch (e) { /* already closed */ } }
      if (form) { form.dataset.appConfirmed = "1"; form.requestSubmit(); }
    }
    if (supported) {
      dlg.querySelector("[data-confirm-ok]").addEventListener("click", function () { settle(true); });
      dlg.querySelector("[data-confirm-cancel]").addEventListener("click", function () { settle(false); });
      dlg.addEventListener("cancel", function (e) { e.preventDefault(); settle(false); });
    }
    document.addEventListener("submit", function (event) {
      const form = event.target;
      if (!form.dataset || !form.dataset.confirm) return;
      if (form.dataset.appConfirmed === "1") { delete form.dataset.appConfirmed; return; }
      event.preventDefault();
      if (!supported) { toast("This action needs a confirmation this browser cannot show.", "error"); return; }
      pending = form;
      dlg.querySelector("[data-confirm-text]").textContent = form.dataset.confirm;
      dlg.querySelector("[data-confirm-ok]").textContent = form.dataset.confirmLabel || "Yes, do it";
      dlg.classList.toggle("is-destructive", form.dataset.confirmKind === "destructive");
      dlg.showModal();
    }, true);
  }

  /* --------------------------------------------------------------- palette
     Ctrl/Cmd-K over an index the APP serves: {"groups": [{"label", "items": [{label, sub, url}]}]}.
     Fetched once, on first open — a palette nobody opens costs nothing. A palette that cannot
     load its index SAYS so; an empty list would read as "nothing matches". */
  function armPalette() {
    const dlg = document.querySelector("[data-palette]");
    if (!dlg || typeof dlg.showModal !== "function") return;
    const input = dlg.querySelector("[data-palette-input]");
    const list = dlg.querySelector("[data-palette-list]");
    const note = dlg.querySelector("[data-palette-note]");
    let data = null, results = [], sel = 0;
    function render() {
      list.textContent = "";
      results.forEach(function (item, i) {
        const li = document.createElement("li");
        li.className = "palette-item" + (i === sel ? " is-selected" : "");
        li.setAttribute("role", "option");
        li.setAttribute("aria-selected", i === sel ? "true" : "false");
        const label = document.createElement("span");
        label.textContent = item.label || "";
        const sub = document.createElement("span");
        sub.className = "muted";
        sub.textContent = [item.group, item.sub].filter(Boolean).join(" · ");
        li.append(label, sub);
        li.addEventListener("click", function () { go(item); });
        list.append(li);
      });
    }
    function filter() {
      const q = input.value.trim().toLowerCase();
      const out = [];
      ((data && data.groups) || []).forEach(function (g) {
        (g.items || []).forEach(function (item) {
          const hay = ((item.label || "") + " " + (item.sub || "")).toLowerCase();
          let i = 0, score = 0, prev = -1;
          for (const ch of q) {
            const at = hay.indexOf(ch, i);
            if (at === -1) { score = -1; break; }
            if (at === prev + 1) score += 2;
            if (at === 0 || hay[at - 1] === " ") score += 3;
            prev = at; i = at + 1;
          }
          if (score >= 0) out.push(Object.assign({}, item, { group: g.label, score: score }));
        });
      });
      out.sort(function (a, b) { return b.score - a.score || (a.label || "").length - (b.label || "").length; });
      results = out.slice(0, 40);
      sel = 0;
      note.hidden = !(data && data.error) && results.length > 0;
      note.textContent = data && data.error ? "The index could not be loaded: " + data.error
        : (results.length ? "" : "Nothing matches “" + input.value + "”.");
      render();
    }
    function go(item) { if (item && item.url) { dlg.close(); window.location.assign(item.url); } }
    async function open() {
      input.value = "";
      dlg.showModal();
      input.focus();
      if (!data) {
        try {
          const r = await fetch(CAP.palette.url, { headers: { Accept: "application/json" }, credentials: "same-origin" });
          if (!r.ok) throw new Error("HTTP " + r.status);
          data = await r.json();
        } catch (e) { data = { groups: [], error: String(e.message || e) }; }
      }
      filter();
    }
    Shell.openPalette = open;
    input.addEventListener("input", filter);
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" && results.length) { sel = (sel + 1) % results.length; render(); e.preventDefault(); }
      else if (e.key === "ArrowUp" && results.length) { sel = (sel - 1 + results.length) % results.length; render(); e.preventDefault(); }
      else if (e.key === "Enter") { go(results[sel]); e.preventDefault(); }
    });
    document.addEventListener("keydown", function (e) {
      if (e.key.toLowerCase() === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        if (dlg.open) dlg.close(); else open();
      }
    });
    document.querySelectorAll("[data-palette-open]").forEach(function (b) { b.addEventListener("click", open); });
  }

  /* ------------------------------------------------------------------ live
     (1) the status reports DISCONNECTED honestly, in words — a stale surface that looks live
     is the failure nobody can see; (2) reconnect with capped backoff, or a restarting server is
     hammered by every open tab; (3) a new build is OFFERED, never reloaded over unsaved work. */
  function armLive() {
    const word = document.querySelector("[data-live-word]");
    const status = document.querySelector("[data-live-status]");
    const offer = document.querySelector("[data-build-offer]");
    let es = null, backoff = 1000, timer = null, lastEdit = 0, newBuild = null;
    function state(live, label) {
      if (status) status.dataset.state = live ? "live" : "down";
      if (word) word.textContent = label;
    }
    function connect() {
      if (es) { try { es.close(); } catch (e) { /* gone */ } }
      try { es = new EventSource(CAP.live.url); }
      catch (e) { state(false, "Offline"); return; }
      es.addEventListener("open", function () { backoff = 1000; state(true, "Live"); });
      es.addEventListener("error", function () {
        try { es.close(); } catch (e) { /* closed */ }
        backoff = Math.min(backoff * 2, 30000);
        state(false, "Reconnecting");
        window.clearTimeout(timer);
        timer = window.setTimeout(connect, backoff);
      });
      es.addEventListener("build", function (event) { onBuild(event.data); });
      es.addEventListener("refresh", function (event) {
        let detail;
        try { detail = JSON.parse(event.data); } catch (e) { detail = { raw: event.data }; }
        document.body.dispatchEvent(new CustomEvent("app-refresh", { detail: detail }));
      });
    }
    function safeToReload() {
      const a = document.activeElement;
      if (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return false;
      if (Date.now() - lastEdit < 5000) return false;
      if (document.querySelector("dialog[open], [data-drawer]:not([hidden]), .htmx-request")) return false;
      return !Array.from(document.querySelectorAll("[data-server-value]"))
        .some(function (el) { return el.value !== el.dataset.serverValue; });
    }
    function onBuild(build) {
      const current = document.body.getAttribute("data-build");
      if (!build || build === current || build === newBuild) return;
      newBuild = build;
      (function attempt() {
        if (safeToReload()) { window.location.reload(); return; }
        if (offer) offer.hidden = false;
        window.setTimeout(attempt, 4000);
      })();
    }
    document.addEventListener("input", function () { lastEdit = Date.now(); }, true);
    const reload = document.querySelector("[data-build-reload]");
    if (reload) reload.addEventListener("click", function () { window.location.reload(); });
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && status && status.dataset.state !== "live") connect();
    });
    connect();
  }

  /* ----------------------------------------------------------- transitions
     Progressive enhancement only: a browser without the API simply swaps. */
  function armTransitions() {
    if (!document.startViewTransition) return;
    document.addEventListener("htmx:beforeSwap", function (event) {
      const detail = event.detail;
      if (!detail || typeof detail.swapCallback !== "function") return;
      const swap = detail.swapCallback;
      detail.swapCallback = function () { return document.startViewTransition(swap); };
    });
  }

  function init() {
    armTheme();
    armKeyboardScrolling();
    armPollPause();
    if (CAP.drawer) armDrawer();
    if (CAP.confirm) armConfirm();
    if (CAP.palette) armPalette();
    if (CAP.live) armLive();
    if (CAP.transitions) armTransitions();
    document.documentElement.setAttribute("data-shell-ready", Object.keys(CAP).sort().join(" ") || "core");
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
