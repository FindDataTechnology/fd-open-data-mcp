/* fd Crawl Control panel behavior: theme toggle + toast region
 * (panel-ui-refresh design D2/D5). Loaded with defer; no dependencies. */
(function () {
  "use strict";

  var KEY = "panel-theme";
  var root = document.documentElement;
  var btn = document.getElementById("theme-toggle");

  function current() {
    return root.dataset.theme ||
      (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark" : "light");
  }

  function paint(theme) {
    root.dataset.theme = theme;
    try { localStorage.setItem(KEY, theme); } catch (e) { /* private mode */ }
    if (btn) {
      btn.textContent = theme === "dark" ? "☀" : "☾";
      btn.setAttribute("aria-label", "切换主题 Toggle theme");
    }
  }

  if (btn) {
    btn.addEventListener("click", function () {
      paint(current() === "dark" ? "light" : "dark");
    });
  }
  paint(current()); // sync the toggle label with the pre-paint script's choice

  // Inline-action handlers announce outcomes through the HX-Trigger response
  // header as a `toast` event whose detail is {message, level}.
  var region = document.getElementById("toast-region");
  function show(detail) {
    if (!region) return;
    var el = document.createElement("div");
    el.className = "toast " + ((detail && detail.level) === "err" ? "err" : "ok");
    el.textContent = (detail && detail.message) || "done";
    region.appendChild(el);
    setTimeout(function () { el.remove(); }, 4000);
  }
  document.body.addEventListener("toast", function (ev) { show(ev.detail); });
})();
