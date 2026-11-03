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
  // ---- #312 health + #318 minimum version (ADR 029 / ADR 030) ------------------------------
  // POST /api/session/app: enums, the app version/build, and for errors only an /api route and a
  // status code. Never message text, a URL query, a token or anything on the screen.
  var INFO = global.__ONCA_APP__ || {};
  var PLATFORM = INFO.platform === "ios" || INFO.platform === "android" ? INFO.platform : "other";
  var sent = 0;
  function report(body) {
    if (sent >= 10) return Promise.resolve(null);            // per page: a broken page can't flood
    sent += 1;
    var payload = { platform: PLATFORM, version: INFO.version, build: INFO.build, bridge: INFO.bridge };
    for (var k in body) payload[k] = body[k];
    try {
      return origFetch("/api/session/app", { method: "POST", credentials: "same-origin", cache: "no-store",
        keepalive: true, headers: { "content-type": "application/json", "x-onca-session": "1" },
        body: JSON.stringify(payload) }).then(function (r) { return r.ok ? r.json() : null; })
        .catch(function () { return null; });
    } catch (e) { return Promise.resolve(null); }
  }
  function apiRoute(input) {
    var u; try { u = new URL(typeof input === "string" ? input : input.url, global.location.href); } catch (e) { return null; }
    return u.origin === global.location.origin && /^\/api\//.test(u.pathname) && !/^\/api\/session\/app/.test(u.pathname)
      ? u.pathname : null;
  }
  var origFetch = global.fetch ? global.fetch.bind(global) : null;
  if (origFetch) {
    global.fetch = function (input, init) {
      var route = apiRoute(input);
      var p = origFetch(input, init);
      if (!route) return p;
      return p.then(function (r) {
        if (r.status >= 500) report({ kind: "error", source: "api", route: route, status: r.status });
        return r;
      }, function (err) {
        if (!(err && err.name === "AbortError")) report({ kind: "error", source: "api", route: route, status: 0 });
        throw err;
      });
    };
  }
  global.addEventListener("error", function () { report({ kind: "error", source: "page" }); });
  global.addEventListener("unhandledrejection", function () { report({ kind: "error", source: "page" }); });

  // one "start" per WebView session (= an app launch); its reply carries the version verdict
  function onVerdict(v) {
    if (!v || !v.update || v.update === "ok") return;
    var store = typeof v.store === "string" && /^https:\/\/(apps\.apple\.com|play\.google\.com)\//.test(v.store) ? v.store : null;
    var show = function () {
      if (v.update === "soft") {
        try { if (localStorage.getItem("onca_update_soft") === new Date().toISOString().slice(0, 10)) return; } catch (e) {}
      }
      var hard = v.update === "hard";
      var box = doc.createElement("div");
      box.className = hard ? "onca-update onca-update-hard" : "onca-update";
      box.setAttribute("role", hard ? "alertdialog" : "status");
      box.innerHTML = '<p class="onca-update-t"></p><div class="onca-update-a"></div>';
      box.querySelector(".onca-update-t").textContent = hard
        ? "Esta vers\u00e3o do app n\u00e3o \u00e9 mais suportada. Atualize o On\u00e7a para continuar."
        : "H\u00e1 uma nova vers\u00e3o do app On\u00e7a.";
      var actions = box.querySelector(".onca-update-a");
      if (store) {
        var a = doc.createElement("a"); a.href = store; a.target = "_blank"; a.rel = "noopener";
        a.className = "onca-update-btn"; a.textContent = "Atualizar"; actions.appendChild(a);
      }
      var b = doc.createElement("button"); b.type = "button"; b.className = "onca-update-sec";
      b.textContent = hard ? "Sair" : "Agora n\u00e3o";
      b.onclick = function () {
        if (hard) { var C = global.OncaCtx; if (C && C.logout) C.logout(); return; }
        try { localStorage.setItem("onca_update_soft", new Date().toISOString().slice(0, 10)); } catch (e) {}
        box.remove();
      };
      actions.appendChild(b);
      doc.body.appendChild(box);
    };
    if (doc.body) show(); else doc.addEventListener("DOMContentLoaded", show);
  }
  var css2 = doc.createElement("style");
  css2.textContent = ".onca-update{position:fixed;left:12px;right:12px;bottom:calc(76px + env(safe-area-inset-bottom));z-index:2147483646;" +
    "background:#1a2226;color:#e8edf0;border:1px solid #2c383e;border-radius:12px;padding:14px 16px;font:15px/1.4 system-ui,sans-serif;box-shadow:0 8px 24px rgba(0,0,0,.4)}" +
    ".onca-update-hard{inset:0;border-radius:0;border:0;display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center;padding:32px;background:#0e1214}" +
    ".onca-update-t{margin:0 0 12px}.onca-update-a{display:flex;gap:12px;flex-wrap:wrap;justify-content:center}" +
    ".onca-update-btn,.onca-update-sec{min-height:44px;padding:0 18px;border-radius:8px;font:600 15px system-ui,sans-serif;display:inline-flex;align-items:center;text-decoration:none;cursor:pointer}" +
    ".onca-update-btn{background:#3ec39a;color:#06231b;border:0}.onca-update-sec{background:transparent;color:#e8edf0;border:1px solid #3a474d}";
  (doc.head || doc.documentElement).appendChild(css2);
  var started = false;
  try { started = sessionStorage.getItem("onca_app_started") === "1"; sessionStorage.setItem("onca_app_started", "1"); } catch (e) {}
  // the verdict holds for the whole app session: a hard block re-appears on every page
  var VKEY = "onca_app_verdict";
  if (!started) report({ kind: "start", source: "page" }).then(function (v) {
    try { if (v) sessionStorage.setItem(VKEY, JSON.stringify({ update: v.update, store: v.store })); } catch (e) {}
    onVerdict(v);
  });
  else { try { onVerdict(JSON.parse(sessionStorage.getItem(VKEY) || "null")); } catch (e) {} }

  global.__oncaAppGuard = { isPurchase: isPurchase, NOTE: NOTE, report: report, onVerdict: onVerdict };
})(typeof window !== "undefined" ? window : this);
