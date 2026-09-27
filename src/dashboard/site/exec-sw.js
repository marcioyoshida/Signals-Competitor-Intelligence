/* Onça /exec service worker (#162). Served from the site ROOT so it can take the "/exec"
   scope (a worker's max scope is its own directory). Registered by /v3/index.html.

   What it caches — and what it must never cache:
   - the app SHELL (page + shared CSS/JS + manifest/icons) in a VERSIONED cache; every shell
     request is network-first, so an online launch always gets the deployed UI and the copy
     is only the offline fallback. Bumping SHELL_VERSION drops old shells on activate.
   - NOTHING under /api/, no feed.json (the operator ?opkey feed), nothing under /v2/admin.
     The per-user offline FEED is not this worker's: context.js keeps it in the
     "onca-user-feed" cache keyed by the user's sub and wipes it on logout.
   Push (#167) is handled below: payload-free — the lock screen never carries content. */
"use strict";
const SHELL_VERSION = "exec-shell-v2";
const SHELL = ["/exec", "/v2/app.css", "/v2/app.js", "/v2/context.js",
  "/v3/manifest.webmanifest", "/v3/icons/icon-192.png", "/v3/icons/apple-touch-icon.png"];

function neverCache(url) {
  return url.origin !== self.location.origin || url.pathname.indexOf("/api/") === 0 ||
    url.pathname.indexOf("feed") !== -1 || url.pathname.indexOf("/v2/admin") === 0 ||
    url.searchParams.has("opkey") || url.searchParams.has("code");
}

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL_VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys
    .filter((k) => k.indexOf("exec-shell-") === 0 && k !== SHELL_VERSION)
    .map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (neverCache(url)) return;                      // straight to the network, untouched
  const isNav = req.mode === "navigate";
  const shellKey = isNav ? "/exec" : (SHELL.indexOf(url.pathname) !== -1 ? url.pathname : null);
  if (!shellKey) return;
  e.respondWith(fetch(req).then((res) => {
    if (res.ok && !url.search) {
      const copy = res.clone();
      caches.open(SHELL_VERSION).then((c) => c.put(shellKey, copy));
    }
    return res;
  }).catch(() => caches.open(SHELL_VERSION).then((c) => c.match(shellKey))
    .then((hit) => hit || Response.error())));
});

/* #167 content-free push. The push message has NO payload by design; the worker asks the
   server only for a COUNT (by an opaque subscription id), and the notification text is fixed.
   The finding itself appears only after the app opens — i.e. after auth. */
self.addEventListener("push", (e) => {
  e.waitUntil((async () => {
    let count = 0, target = "/exec#hoje";
    try {
      const sub = await self.registration.pushManager.getSubscription();
      if (sub) {
        const r = await fetch("/api/push/pending", { method: "POST", cache: "no-store",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ endpoint: sub.endpoint }) });
        if (r.ok) { const j = await r.json(); count = j.count || 0; target = j.target || target; }
      }
    } catch (err) { /* offline or server down: still notify, with no count */ }
    const body = count > 1 ? `${count} novos alertas` : "Abra o painel para ver";
    await self.registration.showNotification("Novo alerta no seu painel Onça", {
      body, icon: "/v3/icons/icon-192.png", badge: "/v3/icons/icon-192.png",
      tag: "onca-alert", renotify: true, data: { target },
    });
  })());
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.target) || "/exec#hoje";
  // opened-count for the operator view (by the opaque endpoint; no content involved)
  self.registration.pushManager.getSubscription().then((sub) => sub && fetch("/api/push/opened", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ endpoint: sub.endpoint }) })).catch(() => {});
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
    for (const w of wins) {
      if (new URL(w.url).pathname.indexOf("/exec") === 0 && "focus" in w) {
        w.postMessage({ type: "onca-open", target });
        return w.focus();
      }
    }
    return self.clients.openWindow(target);
  }));
});
