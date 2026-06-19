/**
 * Assign each control a random camo stack (1–7).
 * If Camouflage_png/stack_N.png exists (served as /camo/stack_N.png on the API host),
 * that texture is used; otherwise seven different filters/offsets apply to the shared SVG tile.
 */
(function () {
  "use strict";

  var STACKS = 7;

  function getApiBase() {
    try {
      var html = document.documentElement;
      var b = (html && html.getAttribute("data-samsel-api-base")) || "";
      b = (b || "").trim();
      if (!b) {
        var m = document.querySelector('meta[name="samsel-api-base"]');
        if (m) b = (m.getAttribute("content") || "").trim();
      }
      return b.replace(/\/$/, "");
    } catch (e) {
      return "";
    }
  }

  function camoStackUrl(n) {
    var api = getApiBase();
    var path = api ? api + "/camo/stack_" + n + ".png" : "/camo/stack_" + n + ".png";
    if (/^https?:\/\//i.test(path)) return path;
    try {
      return new URL(path, window.location.origin).href;
    } catch (e) {
      return path;
    }
  }

  function assignStacks(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll(".btn, .tab, label.btn-file, button[class*='btn-']").forEach(function (el) {
      if (el.hasAttribute("data-camo-stack-fixed")) return;
      if (el.hasAttribute("data-camo-stack")) return;
      el.setAttribute("data-camo-stack", String(1 + Math.floor(Math.random() * STACKS)));
    });
  }

  function probePngStacks() {
    var html = document.documentElement;
    for (var i = 1; i <= STACKS; i++) {
      (function (n) {
        var src = camoStackUrl(n);
        var img = new Image();
        img.onload = function () {
          html.classList.add("camo-stack-" + n + "-ok");
          html.style.setProperty("--camo-bg-" + n, 'url("' + src + '")');
        };
        img.onerror = function () {};
        img.src = src;
      })(i);
    }
  }

  function init() {
    var seen = new Set();
    function run(root) {
      if (!root || seen.has(root)) return;
      seen.add(root);
      assignStacks(root);
    }
    run(document.getElementById("app"));
    run(document.getElementById("automix-app"));
    document.querySelectorAll(".camo-ui-root").forEach(function (el) {
      run(el);
    });
    probePngStacks();
    document.querySelectorAll(".camo-ui-root").forEach(function (root) {
      if (!root || typeof MutationObserver === "undefined") return;
      var mo = new MutationObserver(function () {
        assignStacks(root);
      });
      mo.observe(root, { childList: true, subtree: true });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
