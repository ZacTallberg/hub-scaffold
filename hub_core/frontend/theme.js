/* Hub theme runtime. The tiny no-FOUC head snippet applies the stored choice before paint;
   this file owns controls, persistence, OS changes, and cross-tab synchronization. */
(function (global) {
  "use strict";
  var KEY = "hub-theme", ORDER = ["system", "light", "dark"];
  var root = document.documentElement;
  var mql = global.matchMedia ? global.matchMedia("(prefers-color-scheme: dark)") : null;
  function read() {
    try { var v = localStorage.getItem(KEY); return ORDER.indexOf(v) >= 0 ? v : "system"; }
    catch (e) { return "system"; }
  }
  function resolve(choice) { return choice === "light" || choice === "dark" ? choice : (mql && mql.matches ? "dark" : "light"); }
  function sync(choice) {
    var select = document.getElementById("hub-theme-select");
    if (select && select.value !== choice) select.value = choice;
  }
  function apply(choice) {
    choice = ORDER.indexOf(choice) >= 0 ? choice : "system";
    if (choice === "system") { root.removeAttribute("data-theme"); root.style.colorScheme = "light dark"; }
    else { root.setAttribute("data-theme", choice); root.style.colorScheme = choice; }
    sync(choice);
    root.dispatchEvent(new CustomEvent("hub:themechange", { bubbles: true, detail: { choice: choice, resolved: resolve(choice) } }));
    return resolve(choice);
  }
  function set(choice) {
    choice = ORDER.indexOf(choice) >= 0 ? choice : "system";
    try { localStorage.setItem(KEY, choice); } catch (e) {}
    return apply(choice);
  }
  function cycle() { var i = ORDER.indexOf(read()); return set(ORDER[(i + 1) % ORDER.length]); }
  function wire() {
    var select = document.getElementById("hub-theme-select");
    if (select && !select._hubThemeWired) {
      select._hubThemeWired = true;
      select.addEventListener("change", function () { set(select.value); });
    }
    sync(read());
  }
  if (mql) {
    var osChange = function () { if (read() === "system") apply("system"); };
    if (mql.addEventListener) mql.addEventListener("change", osChange); else if (mql.addListener) mql.addListener(osChange);
  }
  global.addEventListener("storage", function (event) { if (event.key === KEY) apply(read()); });
  global.HubTheme = { KEY: KEY, get: read, set: set, cycle: cycle, apply: apply, resolve: resolve };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire); else wire();
})(typeof window !== "undefined" ? window : this);
