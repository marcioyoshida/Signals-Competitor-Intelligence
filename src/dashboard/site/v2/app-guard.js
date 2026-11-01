/* #258 (App Store 3.1.1 / 3.1.3): inside the store app no page shows a way to buy. The shell
   injects window.__ONCA_APP__ before every page of the WebView loads (mobile/apps/onca bridge.ts
   injectedFlag), so any onssa.org page the app can reach loads this file in <head>:
     - copy marked data-web-only is hidden;
     - every link to a purchase surface becomes the plain note "Gerencie seu plano em onssa.org."
       (no link), including links a script adds later (MutationObserver).
   The purchase matcher is the shell's isPurchaseUrl (bridge.ts): storefront, /pricing,
   /exec?assinar=, an "Assinar" mailto. The shell also refuses those navigations; this removes
   the visible affordance. In a browser this file does nothing. */
(function (global) {
  "use strict";
  var APP = global.__ONCA_APP__ && global.ReactNativeWebView;
  if (!APP) return;
  var NOTE = "Gerencie seu plano em onssa.org.";
  var STORE = { "signals-llc.store": 1, "www.signals-llc.store": 1 };
  var OWN = { "onssa.org": 1, "www.onssa.org": 1 };

  function isPurchase(href) {
    var u;
    try { u = new URL(href, global.location.href); } catch (e) { return false; }
    if (u.protocol === "mailto:") {
      var raw = u.href; try { raw = decodeURIComponent(raw); } catch (e) { /* keep raw */ }
      return /subject=[^&]*assinar/i.test(raw);
    }
    var host = u.hostname.toLowerCase();
    if (STORE[host]) return true;
    // our own pages: the production hosts, or whatever host serves this page (a local test server)
    if (!(OWN[host] || host === global.location.hostname.toLowerCase())) return false;
    if (/^\/pricing(\.html)?$/i.test(u.pathname)) return true;
    return /^\/exec\/?$/i.test(u.pathname) && u.searchParams.has("assinar");
  }

  function scrub(root) {
    if (!root || !root.querySelectorAll) return;
    var links = root.querySelectorAll("a[href]");
    if (root.matches && root.matches("a[href]")) links = [root].concat(Array.prototype.slice.call(links));
    Array.prototype.forEach.call(links, function (a) {
      if (!isPurchase(a.getAttribute("href"))) return;
      var s = global.document.createElement("span");
      s.className = "plan-note";
      s.textContent = NOTE;
      if (a.parentNode) a.parentNode.replaceChild(s, a);
    });
  }

  var doc = global.document;
  doc.documentElement.classList.add("onca-app");
  var css = doc.createElement("style");
  css.textContent = ".onca-app [data-web-only]{display:none!important}";
  (doc.head || doc.documentElement).appendChild(css);
  new MutationObserver(function (muts) {
    muts.forEach(function (m) { Array.prototype.forEach.call(m.addedNodes, function (n) { if (n.nodeType === 1) scrub(n); }); });
  }).observe(doc.documentElement, { childList: true, subtree: true });
  if (doc.readyState !== "loading") scrub(doc.documentElement);
  else doc.addEventListener("DOMContentLoaded", function () { scrub(doc.documentElement); });
  global.__oncaAppGuard = { isPurchase: isPurchase, NOTE: NOTE };
})(typeof window !== "undefined" ? window : this);
