/* Browser-side failure reporting — every surface, automatic, on by default.
 *
 * A Django exception is logged, traced and alertable. A JavaScript exception, a
 * background request that 403s, a socket that dies, a stream that stops
 * reconnecting or a console.error produces NOTHING anywhere: the page keeps
 * rendering and the only signal is a person saying "it doesn't work". The
 * listeners below close that, and they are the whole client side of error
 * visibility.
 *
 * AUTOMATIC IS THE POINT. Nothing here asks an app to call it.
 * Every channel the browser can fail through is hooked at the platform object -
 * XMLHttpRequest, fetch, WebSocket, EventSource, Worker, console.error, CSP -
 * because a channel that needs an app to remember is a channel that is dark on
 * the apps that forgot. window.reportProblem / reportStreamError / reportHandled
 * remain for the failures only the app can name.
 *
 *   <script src="{% static 'app_errors/report_errors.js' %}"
 *           data-report-url="{% url 'errors_report' %}" defer></script>
 *
 * Rows post to the app's own endpoint, which records them locally and forwards them
 * to the hub. Every rule below exists because a real row cried wolf without it:
 * a surface that reports what nobody can act on is one people stop reading.
 *
 * Everything here is defensive: reporting must never be the thing that breaks a
 * page, so every call is wrapped, capped, and deduped client-side too.
 */
(function () {
  var script = document.currentScript ||
      document.querySelector('script[data-report-url]');
  var URL_ = (script && script.getAttribute('data-report-url')) || '/errors/report/';

  var seen = Object.create(null);      // fingerprint -> count, this page load
  var sent = 0;
  var MAX_PER_PAGE = 20;               // a hot loop must not DDoS our own endpoint
  var MAX_PER_FINGERPRINT = 3;

  function post(payload) {
    try {
      if (sent >= MAX_PER_PAGE) return;
      var fp = (payload.kind || '') + '|' + (payload.message || '').slice(0, 200);
      seen[fp] = (seen[fp] || 0) + 1;
      if (seen[fp] > MAX_PER_FINGERPRINT) return;
      sent++;
      payload.page_url = payload.page_url || location.pathname + location.search;
      var body = JSON.stringify(payload);
      // sendBeacon survives the page being torn down mid-navigation, which is
      // exactly when a "this page broke" report is most likely to be lost.
      if (navigator.sendBeacon) {
        navigator.sendBeacon(URL_, new Blob([body], { type: 'application/json' }));
      } else {
        fetch(URL_, { method: 'POST', body: body, keepalive: true,
                      headers: { 'Content-Type': 'application/json' } }).catch(function () {});
      }
    } catch (e) { /* reporting must never throw */ }
  }
  window.reportProblem = post;

  function describeRejection(reason) {
    if (reason == null) return 'rejected with no reason';
    if (typeof reason === 'string') return reason;
    if (reason instanceof Error || typeof reason.message === 'string') {
      var name = reason.name && reason.name !== 'Error' ? reason.name + ': ' : '';
      return name + (reason.message || 'error with no message');
    }
    if (typeof Response !== 'undefined' && reason instanceof Response) {
      return ('HTTP ' + reason.status + ' ' + (reason.statusText || '') + ' ' +
              (reason.url || '')).trim();
    }
    if (typeof reason.status === 'number' && (reason.url || reason.statusText)) {
      return 'HTTP ' + reason.status + ' ' + (reason.url || reason.statusText);
    }
    try {
      var json = JSON.stringify(reason);
      if (json && json !== '{}') {
        return ((reason.constructor && reason.constructor.name) || 'object') +
               ' ' + json.slice(0, 300);
      }
    } catch (_error) { /* circular object: report its shape below */ }
    var keys = Object.keys(reason).slice(0, 12).join(', ');
    return ((reason.constructor && reason.constructor.name) || typeof reason) +
           ' with keys [' + keys + ']';
  }

  // 1. uncaught JS exceptions
  window.addEventListener('error', function (e) {
    // A failed <img>/<script>/<link> fires this too, with no e.error — report it
    // as a resource failure rather than pretending it is a script exception.
    if (e.target && e.target !== window && e.target.tagName) {
      var el = e.target;
      // An element that diagnoses its own failures - re-fetches the URL, reads the
      // status and reason the <img> error threw away, and reports THAT - opts out
      // here with data-reports-own-errors. Without it every such failure posts twice,
      // and this generic row, whose message never changes, folds every cause (a
      // designed 415, a real 502, a 401 sign-in expiry) into one problem that reopens
      // forever.
      if (el.hasAttribute && el.hasAttribute('data-reports-own-errors')) return;
      var url = el.currentSrc || el.src || el.href || '';
      // A CSP-blocked element fires BOTH securitypolicyviolation and this error
      // event, in the same millisecond, violation first. The CSP row names the
      // CAUSE ('img-src refused it'); this one names only the symptom, and their
      // fingerprints differ, so one blocked image would become two rows.
      // In Chromium the violation event's target is DOCUMENT, not
      // the element -- so the two are matched on the blocked URI instead, which is
      // either the full URL or a bare scheme token ('data', 'blob', 'inline').
      if (cspJustBlocked(url)) return;
      // An element cleared with src='' fires this event by spec. That is the page
      // tidying up, not a failed load: nothing was requested, so there is nothing
      // to report. (Clear with removeAttribute('src') to avoid the event entirely.)
      // Keyed on the ATTRIBUTE: the resolved el.src / currentSrc of an empty src is the
      // document's own URL in Chromium, so a check on the resolved URL never matches.
      if (el.hasAttribute && el.hasAttribute('src') && el.getAttribute('src') === '') return;
      // Never post a causeless row: when there is no URL, name the element instead,
      // so the row says WHICH image failed (a bare 'resource failed to load: IMG'
      // row costs a task to trace).
      var who = el.tagName + (el.id ? '#' + el.id : '') +
                (el.className && typeof el.className === 'string' && el.className.trim()
                   ? '.' + el.className.trim().split(/\s+/).join('.') : '');
      post({ kind: 'http', message: 'resource failed to load: ' + who,
             source: url || ('element ' + who + ' with ' +
                             (el.hasAttribute && el.hasAttribute('src')
                                ? 'src="' + (el.getAttribute('src') || '') + '"'
                                : 'no src attribute')) });
      return;
    }
    if (isClaimed(e.message) || isClaimed(e.error && e.error.message)) return;
    post({ kind: 'js',
           message: (e.error && e.error.message) || e.message || 'script error',
           source: [e.filename, e.lineno, e.colno].filter(Boolean).join(':'),
           stack: (e.error && e.error.stack) || '' });
  }, true);                            // capture phase, so resource errors arrive

  // 2. unhandled promise rejections
  window.addEventListener('unhandledrejection', function (e) {
    // A promise rejected with NO reason arrives as undefined. It must reach
    // describeRejection as undefined: substituting {} here is what turned every
    // one of them into a causeless "Object with keys []" row.
    var raw = e.reason;
    // htmx.ajax() rejects its promise with no argument when its request is
    // aborted, errors or times out (bundled htmx 2.0.4: `onabort/onerror/
    // ontimeout -> reject()`), right after firing htmx:sendAbort / sendError /
    // timeout. Those events are already governed below — a page being torn down
    // is ignored, a background blip must repeat — so the bare rejection is the
    // same event a second time with every one of those rules bypassed.
    if (raw == null && Date.now() - htmxTransportEndedAt < 2000) return;
    var reason = raw == null ? {} : raw;
    var text = describeRejection(raw);
    // The fetch wrapper below already reported this one as `http`, with the URL
    // and status in it. Reporting it again here as `promise` is one event, two
    // fingerprints, two rows.
    if (reason && reason.__evReported) return;
    // A view transition superseded by the next swap rejects with AbortError.
    // That is two updates arriving close together — routine, not a failure.
    if (reason.name === 'AbortError' || /[Tt]ransition was (skipped|aborted)/.test(text)) return;
    post({ kind: 'promise', message: text || 'unhandled rejection',
           stack: reason.stack || '' });
  });

  // A 4xx the application ASKED FOR is not a fault. An app that answers a
  // designed outcome with a status code — "this line still has money on it,
  // confirm?", "somebody edited this first, here is their value" — was having
  // every one of those recorded as a failure: a board showing a HIGH error x10
  // that was ten people successfully confirming a deletion. A surface that cries wolf on the
  // product's own UX is one people stop reading, which is the whole thing
  // error visibility exists to prevent. A server marks these with a header;
  // anything unmarked still reports.
  //
  //   return HttpResponse("Remove it anyway?", status=409,
  //                       headers={"X-Handled": "1"})
  //
  // X-Needs-Confirm and X-Live-Conflict are the realtime-grammar component's
  // own two, recognised here so apps using it need no change.
  var HANDLED_HEADERS = ['X-Handled', 'X-Needs-Confirm', 'X-Live-Conflict'];

  function isHandled(xhr) {
    try {
      for (var i = 0; i < HANDLED_HEADERS.length; i++) {
        if (xhr.getResponseHeader(HANDLED_HEADERS[i])) return true;
      }
    } catch (e) { /* header access can throw on aborted requests */ }
    return false;
  }

  // 3. htmx request failures (the ones a toast shows and then forgets)
  //
  // A deploy restart wears 400/502/503/504 for about a minute, and htmx's
  // `every` polls meet it exactly as fetch does. The fetch path (hook 7) asks
  // once more before reporting; without the same rule here the restart that
  // goes quiet on one path keeps filing on the other. Same rules as hook 7: GET/HEAD only, so a 400 on a form a person POSTed still
  // reports at once, and a 500 is never rechecked at all.
  document.body && document.body.addEventListener('htmx:responseError', function (e) {
    var x = e.detail && e.detail.xhr;
    if (!x || x.status === 401) return;      // 401 = the gate redirecting, not a fault
    if (isHandled(x)) return;                // a designed outcome, not a failure
    var cfg = e.detail.requestConfig || {};
    var path = cfg.path || '';
    var verb = cfg.verb || '';
    if (isGateway(x.status) && isIdempotent(verb) && path) {
      confirmGatewayFailure(path, function () {
        post({ kind: 'http', status: x.status,
               message: 'htmx ' + x.status + ' on ' + stripQuery(path) +
                        ' - still failing when asked again ' +
                        GATEWAY_RECHECK_AFTER_MS + 'ms later, so the upstream is' +
                        ' down rather than restarting',
               source: verb });
      });
      return;
    }
    post({ kind: 'http', status: x.status,
           message: 'htmx ' + x.status + ' on ' + path,
           source: verb });
  });
  // htmx:sendError means the request never reached a response at all. THREE
  // different things produce it and only ONE of them is a fault:
  //
  //   1. the page is being torn down (navigation, tab close) — the browser
  //      kills every in-flight XHR by design;
  //   2. one tick of a BACKGROUND POLL lost the link — the next tick succeeds
  //      and no person ever saw anything broken;
  //   3. the surface genuinely cannot reach the server.
  //
  // Reporting all three identically puts "network error on /notifications/"
  // on the board for one lost tick of a background poll, on a page that was
  // working, for an endpoint that was healthy. The hub's own bar says
  // transient transport blips must never reach the queue, and this is the same lesson the X-Handled block above was written
  // for — a surface that cries wolf is one people stop reading.
  //
  // The split is drawn where it can be drawn honestly, and it DROPS NOTHING a
  // person experienced. A request the USER made still reports on its first
  // failure: they watched it fail and that copy exists nowhere else. A
  // background request — htmx passes NO triggeringEvent for `every` and `load`
  // triggers, verified against the bundled htmx — has to fail TWICE IN A ROW on
  // the same path first, which is exactly the difference between a blip and a
  // surface that is actually down. Any response on that path, at any status,
  // proves it is reachable and clears the count.
  var unloading = false;
  function markUnloading() { unloading = true; }
  // When an htmx request last ended without a response (see unhandledrejection).
  var htmxTransportEndedAt = 0;
  ['htmx:sendError', 'htmx:sendAbort', 'htmx:timeout'].forEach(function (name) {
    document.body && document.body.addEventListener(name, function () {
      htmxTransportEndedAt = Date.now();
    }, true);
  });
  window.addEventListener('pagehide', markUnloading, true);
  window.addEventListener('beforeunload', markUnloading, true);

  var POLL_FAILURES_BEFORE_REPORT = 2;
  var pollFails = Object.create(null);       // path -> consecutive send failures

  function requestPath(e) {
    return ((e.detail || {}).requestConfig || {}).path || '';
  }

  document.body && document.body.addEventListener('htmx:afterRequest', function (e) {
    // A status of any kind means the server answered, so the path is reachable.
    var x = (e.detail || {}).xhr;
    if (x && x.status) delete pollFails[requestPath(e)];
  });

  document.body && document.body.addEventListener('htmx:sendError', function (e) {
    if (unloading) return;                   // the page is leaving, not failing
    var path = requestPath(e);
    if ((e.detail || {}).requestConfig && e.detail.requestConfig.triggeringEvent) {
      post({ kind: 'http', message: 'network error on ' + path });
      return;                                // a person was waiting on this one
    }
    // navigator.onLine is only trustworthy in the FALSE direction, and that
    // direction is the one we need: no interface at all is not an app fault.
    if (navigator.onLine === false) return;
    var n = (pollFails[path] = (pollFails[path] || 0) + 1);
    // Exactly at the threshold: one row per outage per path, not one per tick.
    if (n !== POLL_FAILURES_BEFORE_REPORT) return;
    post({ kind: 'http',
           message: 'network error on ' + path + ' (' + n + ' consecutive ' +
                    'background attempts, no response) — this surface is not ' +
                    'reaching the server' });
  });
  document.body && document.body.addEventListener('htmx:swapError', function (e) {
    post({ kind: 'js', message: 'htmx could not swap the response into the DOM',
           source: (((e.detail || {}).requestConfig || {}).path || '') });
  });

  // 4. dead live streams — an SSE that stops reconnecting looks exactly like a
  //    quiet system, which is the most expensive kind of silent failure.
  //
  // But this is the one channel an APP calls by hand, and it was the only one
  // holding none of the discipline every automatic hook below holds. Hook 7
  // drops a rejection when the page is unloading, when there is no network
  // interface at all, and when it is an AbortError; this posted every one of
  // them, on the first occurrence, from a poller that ticks every 15 seconds.
  //
  // ONE row with ONE occurrence of "live stream failed: TypeError: Failed to
  // fetch" is the typical shape. One occurrence is the
  // signature of a blip that recovered — a surface actually down would have
  // reported again on the next tick — and a blip on the shared board is
  // indistinguishable from an outage to the person reading it. So the split
  // hook 3 draws for htmx is drawn here too, and for the same reason: a
  // BACKGROUND caller has to fail TWICE IN A ROW on the same source before it
  // reports, while a caller a person is watching still reports on its first
  // failure, because that copy exists nowhere else.
  var streamFails = Object.create(null);     // source -> consecutive failures
  var STREAM_FAILURES_BEFORE_REPORT = 2;

  function abortish(detail) {
    // The caller hands us a string, so the name is all that survives. An abort
    // is something WE did — closing a panel, superseding a request — never a
    // fault. Hook 7 drops these by err.name; this is the same rule on a string.
    return /AbortError/.test(String(detail || ''));
  }

  window.reportStreamError = function (url, detail, opts) {
    // stripQuery, like every other channel: a live URL carries a fingerprint or
    // an id, and this string goes to the shared board.
    var src = stripQuery(url);
    if (unloading) return;                   // the page is leaving, not failing
    if (abortish(detail)) return;
    // navigator.onLine is only trustworthy in the FALSE direction, and that is
    // the direction we need: no interface at all is not an app fault.
    if (navigator.onLine === false) return;
    if (!(opts && opts.background)) {
      post({ kind: 'stream', source: src,
             message: 'live stream failed: ' + (detail || 'connection lost') });
      return;                                // a person was waiting on this one
    }
    var n = (streamFails[src] = (streamFails[src] || 0) + 1);
    // Exactly at the threshold: one row per outage per source, not one per tick.
    if (n !== STREAM_FAILURES_BEFORE_REPORT) return;
    post({ kind: 'stream', source: src,
           message: 'live stream failed: ' + (detail || 'connection lost') +
                    ' (' + n + ' consecutive background attempts, no response) — ' +
                    'this stream is not reaching the server' });
  };

  // Any answer at all on that source proves it is reachable and clears the
  // count, the same way htmx:afterRequest clears hook 3's. A poller that never
  // calls this can still only ever report its second consecutive failure.
  window.reportStreamOk = function (url) {
    try { delete streamFails[stripQuery(url)]; } catch (e) { }
  };

  // 5. anything the app knows is wrong but recovers from
  window.reportHandled = function (message, extra) {
    post(Object.assign({ kind: 'js', message: String(message || '') }, extra || {}));
  };

  // ===========================================================================
  // AUTOMATIC, FROM HERE DOWN. Everything above was already on by default; what
  // follows closes the channels that used to need an app to remember to call
  // something -- and an app that has to remember is an app that is dark.
  //
  // Apps typically hook htmx:responseError in their OWN javascript to raise a
  // toast -- the user is told and nobody else is. WebSocket, EventSource,
  // XMLHttpRequest, fetch, console.error and CSP violations reach nobody unless
  // they are hooked here, at the platform object.
  // ===========================================================================

  // A few failures surface through two channels at once (a worker exception
  // reaches both the Worker and window.onerror; see hook 12). The channel that can
  // say MORE claims the message, and the generic one stands down for a moment.
  var claimed = Object.create(null);
  function claimMessage(m) {
    try { if (m) claimed[String(m).slice(0, 200)] = Date.now(); } catch (e) { }
  }
  function isClaimed(m) {
    try {
      var t = claimed[String(m || '').slice(0, 200)];
      return !!t && (Date.now() - t) < 4000;
    } catch (e) { return false; }
  }

  // The blocked URI of the most recent CSP violation, so the resource-failure
  // branch in hook 1 can recognise the symptom of a cause hook 11 already reported.
  var lastCsp = { uri: '', at: 0 };
  function noteCspBlock(uri) {
    try { lastCsp = { uri: String(uri || ''), at: Date.now() }; } catch (e) { }
  }
  function cspJustBlocked(url) {
    try {
      if (!lastCsp.at || (Date.now() - lastCsp.at) > 1000) return false;
      var u = String(url || ''), b = lastCsp.uri;
      if (!b) return false;
      // A full URL matches by prefix; a scheme token ('data', 'blob', 'inline')
      // is what Chromium reports for a data: URI and matches by scheme.
      return u.indexOf(b) === 0 || u.indexOf(b + ':') === 0;
    } catch (e) { return false; }
  }

  function stripQuery(u) {
    // A URL is the most likely place for a token or an id to be sitting, and this
    // string is going to the shared board. Same rule capture.py applies server-side.
    try { return String(u || '').split('?')[0].slice(0, 400); } catch (e) { return ''; }
  }

  function handledResponse(r) {
    try {
      for (var i = 0; i < HANDLED_HEADERS.length; i++) {
        if (r.headers && r.headers.get(HANDLED_HEADERS[i])) return true;
      }
    } catch (e) { /* an opaque response refuses header reads */ }
    return false;
  }

  // A GATEWAY status is the one failure that is most often OUR OWN DEPLOY: a
  // 30-second poll catches the service mid-restart, the proxy answers 502
  // because the upstream is down for about a second, and that single tick
  // becomes a queued problem although the endpoint never stopped working for a
  // person -- reproducibly, on every deploy.
  //
  // The htmx path above already refuses to report ONE lost tick of a background
  // poll (POLL_FAILURES_BEFORE_REPORT); fetch() and XHR reported the first one
  // instantly, which is the same cry-wolf failure the X-Handled block was
  // written for. But fetch and XHR carry no triggeringEvent, so "was a person
  // waiting on this?" cannot be answered honestly here. So it is not guessed:
  // the surface is ASKED AGAIN. A retry that answers is a restart, a retry that
  // fails too is an outage, and the difference is measured rather than assumed.
  //
  // The rules that keep this from hiding a real failure:
  //   - only 502/503/504 — the proxy saying it has no upstream right now — and
  //     400, which a restart ALSO wears (several GET polls answering 400 for
  //     the minute a service comes up, then 302/200 again). A 500 is the app itself raising, and reports instantly as it
  //     always has; a 400 on a POST is a person's own request and is never
  //     rechecked (the GET/HEAD rule below), so a real bad request still reports.
  //   - only GET/HEAD. A POST is the person's own work and is never replayed.
  //   - the recheck uses the ORIGINAL fetch, so it cannot report itself or
  //     recurse through hook 7.
  //   - every way the recheck can fail to answer REPORTS. Nothing is dropped
  //     because the check itself broke.
  var GATEWAY_STATUSES = { 400: 1, 502: 1, 503: 1, 504: 1 };
  var GATEWAY_RECHECK_AFTER_MS = 1500;
  var nativeFetch = (typeof window.fetch === 'function') ? window.fetch.bind(window) : null;
  var gatewayPending = Object.create(null);    // path -> a recheck is already in flight

  function isGateway(status) {
    return !!GATEWAY_STATUSES[status];
  }

  // A request that may be asked again without doing anything twice.
  function isIdempotent(method) {
    var m = String(method || 'GET').toUpperCase();
    return m === 'GET' || m === 'HEAD';
  }

  // Ask the same URL once more. `report` runs only if it is still not there.
  function confirmGatewayFailure(url, report) {
    try {
      var path = stripQuery(url);
      if (gatewayPending[path]) return;        // one recheck per path, not one per tick
      gatewayPending[path] = true;
      var settle = function (stillDown) {
        delete gatewayPending[path];
        if (stillDown) { try { report(); } catch (e) { } }
      };
      window.setTimeout(function () {
        try {
          if (unloading) { delete gatewayPending[path]; return; }
          if (!nativeFetch) { settle(true); return; }   // cannot ask: report, never drop
          nativeFetch(url, { credentials: 'same-origin', cache: 'no-store',
                             headers: { 'X-Error-Recheck': '1' } })
            .then(function (r) { settle(!r || isGateway(r.status)); },
                  function () { settle(true); });
        } catch (e) { settle(true); }
      }, GATEWAY_RECHECK_AFTER_MS);
    } catch (e) { try { report(); } catch (_e) { } }
  }

  // The message a confirmed outage carries: it says the check happened, so
  // nobody reading the board has to wonder whether it was a blip.
  function gatewayMessage(status, method, url) {
    return 'HTTP ' + status + ' on ' + method + ' ' + stripQuery(url) +
           ' - still failing when asked again ' + GATEWAY_RECHECK_AFTER_MS +
           'ms later, so the upstream is down rather than restarting';
  }

  // 6. EVERY XMLHttpRequest - htmx, jQuery, and hand-rolled alike.
  //
  // htmx issues XHRs, so this wrapper and the htmx listeners above see the same
  // request. The htmx path is the one that must win: the two-strike poll
  // de-bounce, the 401 skip and the X-Handled check all live up there, and every
  // one of them was written against a real board row that cried wolf. htmx stamps
  // each request it owns with the HX-Request header, so that is the discriminator
  // - version-independent, and part of htmx's wire contract rather than an
  // internal detail that a version bump can move.
  (function () {
    if (typeof XMLHttpRequest === 'undefined' || !XMLHttpRequest.prototype) return;
    var XP = XMLHttpRequest.prototype;
    var open_ = XP.open, send_ = XP.send, setHeader_ = XP.setRequestHeader;

    XP.open = function (method, url) {
      try { this.__ev = { method: String(method || 'GET').toUpperCase(), url: String(url || '') }; }
      catch (e) { /* a frozen XHR is still a working XHR */ }
      return open_.apply(this, arguments);
    };

    XP.setRequestHeader = function (name, value) {
      try {
        if (this.__ev && String(name).toLowerCase() === 'hx-request') this.__ev.hx = true;
      } catch (e) { }
      return setHeader_.apply(this, arguments);
    };

    XP.send = function () {
      var self = this;
      try {
        self.addEventListener('abort', function () { try { self.__evAborted = true; } catch (e) { } });
        self.addEventListener('loadend', function () {
          try {
            var m = self.__ev || {};
            if (m.hx) return;                       // htmx owns it; handled above
            if (unloading) return;                  // the page is leaving, not failing
            var url = m.url || '';
            if (!url || url.indexOf(URL_) === 0) return;   // never report the reporter
            if (self.status === 0) {
              // Aborted is the page's own doing - a superseded search-as-you-type
              // is the common one - and is not a failure anybody experienced.
              if (self.__evAborted) return;
              if (navigator.onLine === false) return;
              post({ kind: 'http',
                     message: 'request failed with no response: ' + m.method + ' ' + stripQuery(url) });
              return;
            }
            if (self.status === 401) return;        // the gate redirecting, not a fault
            if (self.status >= 400 && !isHandled(self)) {
              if (isGateway(self.status) && isIdempotent(m.method)) {
                // The upstream may simply be restarting; ask again before saying so.
                var xhrStatus = self.status, xhrMethod = m.method, xhrUrl = url;
                confirmGatewayFailure(xhrUrl, function () {
                  post({ kind: 'http', status: xhrStatus,
                         message: gatewayMessage(xhrStatus, xhrMethod, xhrUrl) });
                });
                return;
              }
              post({ kind: 'http', status: self.status,
                     message: 'HTTP ' + self.status + ' on ' + m.method + ' ' + stripQuery(url) });
            }
          } catch (e) { }
        });
      } catch (e) { }
      return send_.apply(this, arguments);
    };
  })();

  // 7. EVERY fetch() - the other half of the request surface.
  (function () {
    if (typeof window.fetch !== 'function') return;
    var fetch_ = window.fetch;
    window.fetch = function (input, init) {
      var url = '', method = 'GET';
      try {
        url = (typeof input === 'string') ? input : ((input && input.url) || '');
        method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
      } catch (e) { }
      var p = fetch_.apply(this, arguments);
      try {
        if (!url || url.indexOf(URL_) === 0 || !p || typeof p.then !== 'function') return p;
        return p.then(function (r) {
          try {
            // `ok` is only 2xx, so it is the wrong test: a 304 the page exposed on purpose
            // (a poll that sends its ETag by hand with cache no-store) and a 302 fetched with
            // redirect:'manual' (type 'opaqueredirect', status 0) both answer ok:false and
            // are the request WORKING. A failure is a status of 400 or more, the same floor
            // the XHR wrapper already uses.
            if (r && r.status >= 400 && r.status !== 401 && !handledResponse(r)) {
              if (isGateway(r.status) && isIdempotent(method)) {
                // A background poll that caught our own deploy mid-restart looks
                // exactly like this. Ask the surface again before recording it.
                var status_ = r.status;
                confirmGatewayFailure(url, function () {
                  post({ kind: 'http', status: status_,
                         message: gatewayMessage(status_, method, url) });
                });
                return r;
              }
              post({ kind: 'http', status: r.status,
                     message: 'HTTP ' + r.status + ' on ' + method + ' ' + stripQuery(url) });
            }
          } catch (e) { }
          return r;
        }, function (err) {
          try {
            if (!unloading && navigator.onLine !== false &&
                !(err && err.name === 'AbortError')) {
              post({ kind: 'http',
                     message: 'request failed: ' + method + ' ' + stripQuery(url) + ' - ' +
                              ((err && err.message) || 'network error') });
            }
            // An uncaught fetch rejection ALSO fires unhandledrejection. Without
            // this mark the one failure lands twice, as `http` here and `promise`
            // there, under two different fingerprints - two board rows, one event.
            if (err && typeof err === 'object') err.__evReported = true;
          } catch (e) { }
          throw err;
        });
      } catch (e) { }
      return p;
    };
  })();

  // 8. console.error - the most-used "something is wrong" call in any codebase,
  //    and until now the one place it was guaranteed to stay.
  //
  //    console.warn is deliberately NOT wrapped. Third-party libraries warn about
  //    deprecations on every page load, and the board's own bar is critical/error
  //    from our own apps - a channel that delivers a framework deprecation notice
  //    to a shared queue is one people stop reading, which is the exact failure
  //    this whole component exists to prevent.
  (function () {
    if (!window.console || typeof console.error !== 'function') return;
    var error_ = console.error;
    var inside = false;                  // a console.error inside post() must not recurse

    // htmx narrates every failed request to console.error itself:
    //   "Response Status Error Code 400 from /<prefix>/planning/?... (GET)"
    // Hook 3 above already reports that exact request, WITH the restart recheck
    // and the 401/X-Handled skips. Letting it through too files the same
    // failure twice under two different kinds, and the copy that arrives here
    // carries none of hook 3's judgement -- so it survived a restart recheck
    // that silenced its twin (one request, two rows). Only htmx's own line is dropped; a
    // console.error an app WROTE about a request still reports.
    //
    // htmx also narrates every error-class EVENT by name: its triggerErrorEvent
    // sets detail.error = eventName and logs it, so a request that fails logs a
    // bare "htmx:sendAbort" or "htmx:afterRequest" beside the event itself
    // (one aborted refresh became two rows with no path and no status). The events named here already
    // have a listener in hook 3 that reports them with its judgement, or, for
    // sendAbort, deliberately does not: an abort is htmx cancelling its own
    // request (hx-sync, a newer swap, the page leaving). htmx:timeout,
    // targetError and every other htmx event have no such listener and still
    // report through this hook.
    var HTMX_GOVERNED_EVENTS = /^htmx:(sendAbort|sendError|responseError|swapError|afterRequest)$/;
    function isHtmxOwnNarration(msg) {
      return /^Response Status Error Code \d+ from /.test(msg) ||
             HTMX_GOVERNED_EVENTS.test(msg);
    }

    function describeArg(a) {
      try {
        if (a == null) return String(a);
        if (typeof a === 'string') return a;
        if (a instanceof Error) return (a.name || 'Error') + ': ' + (a.message || '');
        if (typeof a === 'object') return describeRejection(a);
        return String(a);
      } catch (e) { return '[unprintable]'; }
    }
    console.error = function () {
      try {
        if (!inside) {
          inside = true;
          var parts = [], stack = '';
          for (var i = 0; i < arguments.length && i < 6; i++) {
            parts.push(describeArg(arguments[i]));
            if (!stack && arguments[i] instanceof Error) stack = arguments[i].stack || '';
          }
          var msg = parts.join(' ').replace(/\s+/g, ' ').trim().slice(0, 600);
          if (msg && !isHtmxOwnNarration(msg)) {
            post({ kind: 'js', message: 'console.error: ' + msg, stack: stack });
          }
        }
      } catch (e) { } finally { inside = false; }
      return error_.apply(console, arguments);
    };
  })();

  // 9. WebSocket - a live socket that dies is a dead surface that still looks fine.
  (function () {
    if (typeof window.WebSocket !== 'function') return;
    var WS = window.WebSocket;
    function ReportingWebSocket(url, protocols) {
      var ws = (arguments.length > 1) ? new WS(url, protocols) : new WS(url);
      try {
        ws.addEventListener('error', function () {
          if (unloading) return;
          post({ kind: 'stream', message: 'websocket error', source: stripQuery(url) });
        });
        ws.addEventListener('close', function (e) {
          // 1000 is a normal close and 1001 is the page going away. Every other
          // code is the stream dying underneath a page that is still open.
          if (unloading || !e || e.code === 1000 || e.code === 1001) return;
          post({ kind: 'stream', source: stripQuery(url),
                 message: 'websocket closed unexpectedly (code ' + e.code +
                          (e.reason ? ', ' + String(e.reason).slice(0, 120) : '') + ')' });
        });
      } catch (e) { }
      return ws;
    }
    try {
      ReportingWebSocket.prototype = WS.prototype;
      ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED'].forEach(function (k) {
        try { ReportingWebSocket[k] = WS[k]; } catch (e) { }
      });
      window.WebSocket = ReportingWebSocket;
    } catch (e) { window.WebSocket = WS; }
  })();

  // 10. EventSource - automatic now. reportStreamError() below still exists and
  //     still works, but it required an app to remember, and "an SSE that stops
  //     reconnecting looks exactly like a quiet system" is precisely the failure
  //     nobody remembers to wire for.
  (function () {
    if (typeof window.EventSource !== 'function') return;
    var ES = window.EventSource;
    function ReportingEventSource(url, config) {
      var es = (arguments.length > 1) ? new ES(url, config) : new ES(url);
      try {
        es.addEventListener('error', function () {
          if (unloading) return;
          // EventSource retries by itself. A retry in flight (CONNECTING) is the
          // browser doing its job, and reporting it would put a row on the board
          // every time a laptop wifi hiccups. CLOSED is the state that means it
          // has GIVEN UP, and that is the one nobody ever finds out about.
          if (es.readyState !== 2) return;
          post({ kind: 'stream', source: stripQuery(url),
                 message: 'live stream closed and will not reconnect' });
        });
      } catch (e) { }
      return es;
    }
    try {
      ReportingEventSource.prototype = ES.prototype;
      ['CONNECTING', 'OPEN', 'CLOSED'].forEach(function (k) {
        try { ReportingEventSource[k] = ES[k]; } catch (e) { }
      });
      window.EventSource = ReportingEventSource;
    } catch (e) { window.EventSource = ES; }
  })();

  // 11. Content-Security-Policy violations - `other`, and genuinely other: the
  //     page did not throw, no request failed, the browser simply refused to load
  //     something. A tightened header that breaks one app inline script is
  //     invisible in every channel above.
  document.addEventListener('securitypolicyviolation', function (e) {
    try {
      noteCspBlock(e.blockedURI);
      post({ kind: 'other',
             message: 'content security policy blocked ' +
                      (e.violatedDirective || e.effectiveDirective || 'a resource') + ': ' +
                      (stripQuery(e.blockedURI) || 'inline'),
             source: (e.sourceFile ? stripQuery(e.sourceFile) : '') +
                     (e.lineNumber ? ':' + e.lineNumber : '') });
    } catch (_e) { }
  });

  // 12. Workers - a Worker or ServiceWorker that throws takes its exception with
  //     it. window.onerror never sees it; it has its own global scope.
  (function () {
    if (typeof window.Worker === 'function') {
      var W = window.Worker;
      var ReportingWorker = function (url, options) {
        var w = (arguments.length > 1) ? new W(url, options) : new W(url);
        try {
          w.addEventListener('error', function (e) {
            // An uncaught worker exception ALSO reaches window.onerror in Chromium,
            // so this row and hook 1 describe one throw under two fingerprints
            // (observed in Chromium). This row is the better one -- it says
            // WHICH worker -- so it claims the message and hook 1 stands down.
            // preventDefault() is deliberately not used: suppressing the browser's
            // own console report would be this component changing app behaviour.
            var m = (e && e.message) || 'threw with no message';
            claimMessage(m);
            post({ kind: 'js', message: 'worker error: ' + m,
                   source: stripQuery((e && e.filename) || url) });
          });
          w.addEventListener('messageerror', function () {
            post({ kind: 'data', source: stripQuery(url),
                   message: 'worker message could not be deserialized' });
          });
        } catch (e) { }
        return w;
      };
      try {
        ReportingWorker.prototype = W.prototype;
        window.Worker = ReportingWorker;
      } catch (e) { window.Worker = W; }
    }
    try {
      if (navigator.serviceWorker) {
        navigator.serviceWorker.addEventListener('error', function (e) {
          post({ kind: 'js',
                 message: 'service worker error: ' + ((e && e.message) || 'threw with no message') });
        });
        navigator.serviceWorker.addEventListener('messageerror', function () {
          post({ kind: 'data', message: 'service worker message could not be deserialized' });
        });
      }
    } catch (e) { }
  })();
})();
