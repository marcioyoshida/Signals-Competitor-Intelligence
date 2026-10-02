/* ============================================================================
   Onça Design System v2 — shared CONTEXT-SCREEN runtime.
   ----------------------------------------------------------------------------
   app.js stays "pure helpers + primitives only" (its own contract). This file
   is the layer above it: the reusable PANEL compositions + auth + scoped-feed
   loader shared by the five buyer-facing context screens (newentry, adquirencia,
   fintech, seguros, wealth). Admin does NOT load this file — it is operator-side
   and keeps its own inline render.

   ONE system, six dashboards: every panel here is built from the same app.css
   component vocabulary (panel / exc / covrow / qrow / tile / badge / sparkline /
   citationFooter) as the admin reference screen. A screen differs only by which
   panels it LEADS with and the (server-scoped) feed it is handed.

   Honesty invariants carried through every panel:
     - Every synthesized claim shows its clickable source (citationFooter); a card
       with no http citation is labelled "sem fonte", never silently dropped.
     - A derived/inferred value carries the dashed `badge--infer` "inferência"
       label — NEVER shown as a sourced fact. Counts are counts (cited), never a
       market-size estimate.
     - Missing data is an honest empty state, never a fabricated number.
     - Scoped feeds carry NO integrity/reviews/proposals — operator panels simply
       do not exist on these screens (data-stripped server-side, ADR 002).
   ============================================================================ */
(function (global) {
  "use strict";
  const U = global.OncaUI;
  const esc = U.esc;

  /* ======================================================================
     AUTH — Cognito Hosted UI, Authorization Code + PKCE (public SPA client).
     Lifted verbatim in behaviour from the live site so the SaaS screens share
     one login path: login -> id_token -> GET /api/feed with a Bearer token.
     The token is DISPLAY-decoded only; the API's JWT authorizer is the real
     trust boundary (server-side scoping, ADR 002).
     ====================================================================== */
  const AUTH = {
    domain: "https://onca-668449743071.auth.us-east-1.amazoncognito.com",
    clientId: "7qlquhh56o06tp9bo8gp77p385",
    apiUrl: "", // same origin — /api/feed is fronted by CloudFront -> HTTP API (JWT)
    redirectUri: location.origin + location.pathname,
  };
  const TOKEN_KEY = "onca_id_token", VERIFIER_KEY = "onca_pkce_verifier";

  function b64UrlEncode(bytes) {
    let bin = ""; bytes.forEach((b) => { bin += String.fromCharCode(b); });
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function randomPkceVerifier() {
    const arr = new Uint8Array(64); crypto.getRandomValues(arr); return b64UrlEncode(arr);
  }
  async function pkceChallenge(verifier) {
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
    return b64UrlEncode(new Uint8Array(digest));
  }
  function decodeJwt(token) {
    try {
      const b64 = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
      const json = decodeURIComponent(atob(b64).split("").map(
        (c) => "%" + ("00" + c.charCodeAt(0).toString(16)).slice(-2)).join(""));
      return JSON.parse(json);
    } catch (e) { return null; }
  }
  function getIdToken() { try { return sessionStorage.getItem(TOKEN_KEY); } catch (e) { return null; } }
  function isLoggedIn() {
    const t = getIdToken(); if (!t) return false;
    const p = decodeJwt(t); return !!(p && p.exp && p.exp * 1000 > Date.now());
  }
  // A federated (Google) login carries an `identities` claim on the ID token; a
  // native Cognito password login never does. Used to decide whether an
  // authenticated-but-unprovisioned identity gets the self-registration form
  // (Google) or the "talk to us" gate (password — never self-serve, ADR 016).
  function isFederated() {
    if (!isLoggedIn()) return false;
    const p = decodeJwt(getIdToken()) || {};
    return !!p.identities;
  }
  // `idp`, when a string (e.g. "Google"), skips Cognito's own hosted chooser page
  // and goes straight to that identity provider (`identity_provider=` on
  // /oauth2/authorize) — used after self-registration to silently re-auth through
  // Google (session cookie already live) rather than showing the chooser again.
  // Guarded with typeof because this function is also wired directly as an
  // onclick handler, which would otherwise pass the click MouseEvent as `idp`.
  async function login(idp) {
    const useIdp = typeof idp === "string" ? idp : null;
    // #166/#170: a deep link (#hoje-…, ?c=…) must survive the login redirect — Cognito only
    // returns to the bare redirect_uri.
    try {
      const back = location.search.replace(/[?&](code|state)=[^&]*/g, "") + location.hash;
      if (back && back !== "?") sessionStorage.setItem("onca_return", back);
    } catch (e) {}
    const verifier = randomPkceVerifier();
    sessionStorage.setItem(VERIFIER_KEY, verifier);
    const challenge = await pkceChallenge(verifier);
    const params = new URLSearchParams({
      client_id: AUTH.clientId, response_type: "code", scope: "openid email",
      redirect_uri: AUTH.redirectUri, code_challenge: challenge, code_challenge_method: "S256",
    });
    if (useIdp) params.set("identity_provider", useIdp);
    const path = useIdp ? "oauth2/authorize" : "login";
    location.href = `${AUTH.domain}/${path}?${params.toString()}`;
  }
  /* ---- Session broker (#161/#164) -----------------------------------------
     The Cognito refresh token never touches page JavaScript: /api/session/exchange does
     the PKCE code exchange server-side and keeps it in an HttpOnly, SameSite=Strict
     cookie scoped to /api/session; /api/session/refresh trades it for a fresh 1h ID
     token (sliding, per-tenant idle limit); /api/session/logout revokes it. Only the
     short-lived ID token lives here, in sessionStorage. */
  const USER_FEED_CACHE = "onca-user-feed";
  const DEVICE_SESSION_KEY = "onca_u_session";   // {sub, last_active, idle_days} — no token
  function sessionPost(path, body) {
    return fetch("/api/session/" + path, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "content-type": "application/json", "x-onca-session": "1" },
      body: JSON.stringify(body || {}),
    });
  }
  function markDeviceSession(idleDays) {
    const p = decodeJwt(getIdToken() || "") || {};
    if (!p.sub) return;
    let prev = {};
    try { prev = JSON.parse(localStorage.getItem(DEVICE_SESSION_KEY) || "{}"); } catch (e) {}
    const rec = { sub: p.sub, last_active: Date.now(),
      idle_days: idleDays || (prev.sub === p.sub && prev.idle_days) || 7 };
    try { localStorage.setItem(DEVICE_SESSION_KEY, JSON.stringify(rec)); } catch (e) {}
  }
  // Every trace of the signed-in user on this device: token, offline feed, per-user
  // prefs ("onca_u_*" keys), the device-session marker. Called on logout AND when the
  // server says the session is over (idle limit, operator remote sign-out).
  async function clearUserData() {
    // #167: this device stops receiving the user's pushes (the server prunes the now-dead
    // subscription at its next send; logout also deletes it server-side first)
    try {
      if (global.navigator && navigator.serviceWorker) {
        const reg = await navigator.serviceWorker.getRegistration("/exec");
        const ps = reg && reg.pushManager ? await reg.pushManager.getSubscription() : null;
        if (ps) await ps.unsubscribe();
      }
    } catch (e) {}
    try { sessionStorage.removeItem(TOKEN_KEY); } catch (e) {}
    try {
      Object.keys(localStorage).filter((k) => k.indexOf("onca_u_") === 0)
        .forEach((k) => localStorage.removeItem(k));
    } catch (e) {}
    try { if (global.caches) await caches.delete(USER_FEED_CACHE); } catch (e) {}
    try {
      if (global.indexedDB && indexedDB.databases) {
        (await indexedDB.databases()).filter((d) => d.name && d.name.indexOf("onca-u") === 0)
          .forEach((d) => indexedDB.deleteDatabase(d.name));
      }
    } catch (e) {}
  }
  let _refreshing = null, _noSession = false;
  // Resolve a usable ID token: "ok" (valid now), "ended" (no/ended server session — user
  // data wiped), or "offline" (couldn't reach the broker; nothing wiped).
  function ensureSession() {
    if (isLoggedIn()) { markDeviceSession(); return Promise.resolve("ok"); }
    if (_noSession) return Promise.resolve("ended");   // asked once this page: none
    if (_refreshing) return _refreshing;
    _refreshing = (async () => {
      let r;
      try { r = await sessionPost("refresh"); }
      catch (e) { return "offline"; }
      let j = {};
      if (r.ok) {
        j = await r.json().catch(() => ({}));
        if (j.id_token) {
          sessionStorage.setItem(TOKEN_KEY, j.id_token);
          markDeviceSession(j.idle_days);
          return "ok";
        }
      }
      if (r.status === 401 || r.status === 403 || j.session === "none") {
        _noSession = true;
        let had = false;
        try { had = !!localStorage.getItem(DEVICE_SESSION_KEY); } catch (e) {}
        if (had) await clearUserData();
        return "ended";
      }
      return "offline";
    })().finally(() => { _refreshing = null; });
    return _refreshing;
  }
  async function logout() {
    try { if (global.__oncaBeforeLogout) await global.__oncaBeforeLogout(); } catch (e) {}
    try { await sessionPost("logout"); } catch (e) {}
    await clearUserData();
    const params = new URLSearchParams({ client_id: AUTH.clientId, logout_uri: AUTH.redirectUri });
    location.href = `${AUTH.domain}/logout?${params.toString()}`;
  }
  async function exchangeCodeForToken(code) {
    const verifier = sessionStorage.getItem(VERIFIER_KEY);
    sessionStorage.removeItem(VERIFIER_KEY);
    if (!verifier) return false;
    try {
      const r = await sessionPost("exchange",
        { code, code_verifier: verifier, redirect_uri: AUTH.redirectUri });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const j = await r.json();
      if (!j.id_token) throw new Error("sem id_token");
      // a different person on this device: nothing of the previous user may survive
      let prev = {};
      try { prev = JSON.parse(localStorage.getItem(DEVICE_SESSION_KEY) || "{}"); } catch (e) {}
      const sub = (decodeJwt(j.id_token) || {}).sub;
      if (prev.sub && prev.sub !== sub) await clearUserData();
      sessionStorage.setItem(TOKEN_KEY, j.id_token);
      markDeviceSession(j.idle_days);
      return true;
    } catch (e) { console.error("Onça auth: troca de código falhou", e); return false; }
  }
  async function handleAuthCallback() {
    const url = new URL(location.href);
    const code = url.searchParams.get("code");
    if (!code) return;
    await exchangeCodeForToken(code);
    url.searchParams.delete("code"); url.searchParams.delete("state");
    let back = "";
    try { back = sessionStorage.getItem("onca_return") || ""; sessionStorage.removeItem("onca_return"); } catch (e) {}
    history.replaceState({}, "", url.pathname + (back || ((url.search || "") + url.hash)));
  }
  // Header auth affordance: signed-in email + Sair, or Entrar.
  function renderAuthBox(elId, onChange) {
    const el = document.getElementById(elId); if (!el) return;
    if (isLoggedIn()) {
      const p = decodeJwt(getIdToken()) || {};
      el.innerHTML = `<span class="sub" title="${esc(p.email || "conta")}">${esc(p.email || "conta")}</span>
        <button class="btn btn--sm" id="__logoutBtn">Sair</button>`;
      document.getElementById("__logoutBtn").onclick = logout;
    } else {
      el.innerHTML = `<button class="btn btn--primary btn--sm" id="__loginBtn">Entrar</button>`;
      document.getElementById("__loginBtn").onclick = login;
    }
    if (onChange) onChange(isLoggedIn());
  }

  /* ======================================================================
     SCOPED FEED LOADER (ADR 002: the client never filters a superset).
       - entry screen  -> static /feed.entry.json (already scoped, no auth)
       - SaaS screens  -> GET /api/feed with the Bearer token (server-scoped)
       - ?demo=1       -> co-located ./feed.json (harness only; a screen-local
                          synthetic SCOPED slice — never the root full feed, so
                          the demo path cannot leak cross-industry data either)
     Never falls back to the root feed.json on a SaaS screen — a 403 (operator/
     unprovisioned) yields an honest no-access state, not the full feed.
     ====================================================================== */
  async function fetchJson(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw Object.assign(new Error("HTTP " + r.status), { status: r.status });
    return r.json();
  }
  async function loadScopedFeed(opts) {
    opts = opts || {};
    const demo = new URLSearchParams(location.search).get("demo");
    if (demo) {
      try { return { data: await fetchJson("./feed.json") }; }
      catch (e) { return { error: "demo feed indisponível (" + (e.status || e.message) + ")" }; }
    }
    if (opts.entry) {
      // Entry Portal: the static entry slice. No auth, no higher tier reachable.
      try { return { data: await fetchJson(opts.entryUrl || "/feed.entry.json") }; }
      catch (e) { return { error: "feed de entrada indisponível" }; }
    }
    // SaaS: server-authoritative per-tenant scoping.
    const st = await ensureSession();
    if (st === "ended") return { needAuth: true };
    if (st === "offline" && !isLoggedIn()) {
      const cached = await readUserFeed();
      return cached ? { data: cached.data, offline: true, cachedAt: cached.at } : { needAuth: true, offline: true };
    }
    try {
      const r = await fetch("/api/feed", { cache: "no-store",
        headers: { authorization: `Bearer ${getIdToken()}` } });
      if (r.status === 401) { try { sessionStorage.removeItem(TOKEN_KEY); } catch (e) {} return { needAuth: true }; }
      if (r.status === 403) {
        const j = await r.json().catch(() => ({}));
        return { noAccess: true, billing: j.billing || null, upgrade: j.upgrade || null };
      }
      if (!r.ok) return { error: "feed indisponível (HTTP " + r.status + ")" };
      const data = await r.json();
      writeUserFeed(data);
      return { data };
    } catch (e) {
      // #162: offline / weak signal — the LAST feed this same user loaded, flagged as such
      const cached = await readUserFeed();
      if (cached) return { data: cached.data, offline: true, cachedAt: cached.at };
      return { error: "falha de rede ao carregar o feed" };
    }
  }

  /* ---- #162 per-user offline feed ---------------------------------------------
     One cache entry, keyed by the user's `sub`; writing it for user B deletes user A's.
     Read back only for the same `sub` — from the live token, or (installed app launched
     offline) from the device-session marker while it is inside its idle window. Wiped by
     clearUserData() on logout / server-ended session. The operator (?opkey) feed never
     comes through here. */
  function currentSub() {
    const p = decodeJwt(getIdToken() || "") || {};
    if (p.sub) return p.sub;
    try {
      const m = JSON.parse(localStorage.getItem(DEVICE_SESSION_KEY) || "{}");
      if (m.sub && m.last_active && Date.now() - m.last_active < (m.idle_days || 7) * 86400000) return m.sub;
    } catch (e) {}
    return null;
  }
  async function writeUserFeed(data) {
    const sub = currentSub();
    if (!sub || !global.caches) return;
    try {
      const c = await caches.open(USER_FEED_CACHE);
      for (const k of await c.keys()) { if (!k.url.endsWith("/__feed/" + sub)) await c.delete(k); }
      await c.put("/__feed/" + sub, new Response(JSON.stringify({ at: new Date().toISOString(), data }),
        { headers: { "content-type": "application/json" } }));
    } catch (e) { /* quota / private mode: offline copy is best-effort */ }
  }
  async function readUserFeed() {
    const sub = currentSub();
    if (!sub || !global.caches) return null;
    try {
      const c = await caches.open(USER_FEED_CACHE);
      const r = await c.match("/__feed/" + sub);
      return r ? await r.json() : null;
    } catch (e) { return null; }
  }

  // Mirrors tenant_config.ENTRY_INDUSTRIES (Python is the source of truth; the
  // server rejects anything outside this set regardless of what's picked here —
  // same duplication convention infra/app.py's _INDUSTRY_GROUPS already uses).
  const ENTRY_INDUSTRIES = ["agri-funds", "betting", "consorcio", "crypto", "real-estate-funds"];

  // Lazy Entry-tier self-registration (see docs/google-oauth-runbook.md): the one
  // user-initiated write that turns an authenticated-but-unprovisioned Google
  // login into a real (entry-tier) tenant. After it succeeds the CURRENT id token
  // still has no custom:tenant claim (lambda_pretoken.py only resolves it at the
  // NEXT token issuance) — silently re-auth straight through Google (no chooser
  // page) to pick up a fresh token that carries it.
  async function completeRegistration(industries) {
    const r = await fetch("/api/register", {
      method: "POST",
      headers: { authorization: `Bearer ${getIdToken()}`, "content-type": "application/json" },
      body: JSON.stringify({ industries }),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.error || ("HTTP " + r.status));
    }
    try { sessionStorage.removeItem(TOKEN_KEY); } catch (e) {}
    await login("Google");
  }

  /* ---- #187 upgrade path -------------------------------------------------------
     Entry is a paid plan with a 14-day trial; SaaS is sold per module (ADR 024 bands). A CTA
     links to the storefront ONLY when the server says the storefront can fulfil it
     (`upgrade.live`, ONCA_UPGRADE_LIVE on /api/feed) — before that a checkout can't attach to
     the tenant and the customer would pay for nothing, so the CTA is a contact e-mail instead.
     `ref` is the opaque tenant id from the token, never an e-mail. */
  const SAAS_BANDS = {
    saas_premium: ["banking", "investment-banking"],
    saas_mid: ["insurance", "asset-management", "wealth-management", "financial-data-analytics",
               "acquiring", "agri-funds"],
    saas_entry: ["fintech", "advisory", "betting", "crypto", "consorcio", "real-estate-funds"],
  };
  function bandOf(module) {
    return Object.keys(SAAS_BANDS).find((b) => SAAS_BANDS[b].indexOf(module) >= 0) || null;
  }
  function tenantRef() {
    const p = decodeJwt(getIdToken()) || {};
    return String(p["custom:tenant"] || "");
  }
  // Curated combos (ADR 024 amendment 2026-10-02) come from the server (`upgrade.combos`, built
  // from tenant_config.COMBOS) — the page keeps no copy. For tier "saas_combo", `module` is the
  // combo id and the link carries `combo=` instead of `module=`.
  function combosOf(upgrade) { return (upgrade && upgrade.combos) || {}; }
  function comboOf(module, upgrade) {
    const all = combosOf(upgrade);
    const id = Object.keys(all).find((c) => (all[c].modules || []).indexOf(module) >= 0);
    return id ? Object.assign({ id }, all[id]) : null;
  }
  function upgradeHref(tier, module, upgrade, ref) {
    ref = ref || tenantRef();
    const combo = tier === "saas_combo" ? combosOf(upgrade)[module] : null;
    const ok = upgrade && upgrade.live && /^[A-Za-z0-9_-]{1,64}$/.test(ref) && module
      && (tier === "saas_combo" ? !!combo
        : tier === "entry" ? ENTRY_INDUSTRIES.indexOf(module) >= 0 : (SAAS_BANDS[tier] || []).indexOf(module) >= 0);
    if (!ok) {
      const what = tier === "saas_combo" ? "Combo " + (combo ? combo.name : module)
        : (tier === "entry" ? "Entry" : "SaaS") + (module ? " · " + indLabel(module) : "");
      return "mailto:contato@onssa.org?subject=" + encodeURIComponent(`Assinar Onça — ${what}`);
    }
    const q = new URLSearchParams(Object.assign({ product: "onca", tier },
      tier === "saas_combo" ? { combo: module } : { module },
      { ref, return: location.origin + "/exec?upgraded=1" }));
    return String(upgrade.storefront || "https://signals-llc.store").replace(/\/$/, "") + "/?" + q;
  }
  // "Teste grátis: N dias restantes · Assinar" — only for a storefront-managed tenant in trial.
  function billingBannerHTML(data) {
    const b = (data && data.billing) || null;
    if (!b || b.state !== "trial" || !b.trial_until) return "";
    const days = Math.max(0, Math.ceil((new Date(b.trial_until) - Date.now()) / 86400000));
    const mod = (b.trial_modules || [])[0] || "";
    return `<div class="trialbar" role="status"><span>Teste grátis: <b>${days}</b> ${days === 1 ? "dia restante" : "dias restantes"}
      ${mod ? "· " + esc(indLabel(mod)) : ""}</span>
      <a class="btn btn--primary btn--sm" href="${esc(upgradeHref("entry", mod, data.upgrade))}">Assinar Entry · R$ 490/mês</a></div>`;
  }

  // A full-panel login/no-access gate (honest, never a silent empty grid).
  function gateHTML(kind, ctxLabel, res) {
    if (kind === "needAuth") {
      return `<div class="empty"><div class="em-ico" aria-hidden="true">◐</div>
        <div class="em-t">Entre para ver ${esc(ctxLabel)}</div>
        <div class="em-d">O feed é escopado no servidor à sua licença. Nada é filtrado no navegador.</div>
        <div style="margin-top:var(--s3)"><button class="btn btn--primary" id="__gateLogin">Entrar</button></div></div>`;
    }
    if (kind === "noAccess") {
      const bill = (res && res.billing) || null;
      if (bill && (bill.state === "trial_expired" || bill.state === "lapsed")) {
        const ended = bill.state === "trial_expired";
        const mods = ended ? (bill.trial_modules || []) : (bill.lapsed_modules || []);
        const combos = ended ? [] : (bill.lapsed_combos || []);
        const ctas = (mods.length || combos.length ? mods : [""]).map((m) => {
          const band = (bill.bands || {})[m];
          const tier = ended || !band ? "entry" : band;
          return `<a class="btn btn--primary" href="${esc(upgradeHref(tier, m, res.upgrade))}">Assinar${m ? " " + esc(indLabel(m)) : ""}</a>`;
        }).concat(combos.map((c) => {
          const co = combosOf(res.upgrade)[c];
          return `<a class="btn btn--primary" href="${esc(upgradeHref("saas_combo", c, res.upgrade))}">Assinar combo ${esc(co ? co.name : c)}</a>`;
        })).join(" ");
        return `<div class="empty"><div class="em-ico" aria-hidden="true">◐</div>
          <div class="em-t">${ended ? "Seu teste grátis terminou" : "Sua assinatura não está ativa"}</div>
          <div class="em-d">${ended ? "Os 14 dias de teste acabaram." : "O pagamento da assinatura foi encerrado."}
            Assine para voltar a ver ${esc(ctxLabel)}. Nada do seu histórico foi apagado.</div>
          <div style="margin-top:var(--s3)">${ctas}</div>
          <div class="em-d" style="margin-top:var(--s2)"><a href="/pricing.html">Ver planos</a></div></div>`;
      }
      if (isFederated()) {
        return `<div class="empty"><div class="em-ico" aria-hidden="true">◐</div>
          <div class="em-t">Comece seu teste grátis de 14 dias</div>
          <div class="em-d">Sua conta Google ainda não está vinculada a um plano. Escolha o setor que
            quer acompanhar no plano Entry (R$ 490/mês depois do teste, um setor). Não pedimos cartão agora.</div>
          <form id="__regForm" style="margin-top:var(--s3);text-align:left;max-width:340px;margin-inline:auto">
            ${ENTRY_INDUSTRIES.map((s) => `<label class="cvsub" style="display:block;margin:6px 0">
              <input type="radio" name="ind" value="${esc(s)}"> ${esc(indLabel(s))}</label>`).join("")}
            <div id="__regErr" class="badge badge--crit" style="display:none;margin-top:var(--s2)"></div>
            <button type="submit" class="btn btn--primary" style="margin-top:var(--s3)">Começar teste grátis</button>
          </form></div>`;
      }
      return `<div class="empty"><div class="em-ico" aria-hidden="true">⦸</div>
        <div class="em-t">Sua conta não licencia este contexto</div>
        <div class="em-d">Este painel só renderiza o feed que o servidor entrega para a sua licença.
          Fale com o time Onça para habilitar ${esc(ctxLabel)}.</div></div>`;
    }
    return U.empty("Feed indisponível", kind || "Não foi possível carregar o feed.", "!");
  }
  function mountGate(el, kind, ctxLabel, res) {
    el.innerHTML = gateHTML(kind, ctxLabel, res);
    const b = el.querySelector("#__gateLogin");
    if (b) b.onclick = login;
    const f = el.querySelector("#__regForm");
    if (f) f.addEventListener("submit", async (e) => {
      e.preventDefault();
      const picked = Array.from(f.querySelectorAll('input[name="ind"]:checked')).map((i) => i.value);
      const err = f.querySelector("#__regErr");
      const btn = f.querySelector('button[type="submit"]');
      if (picked.length !== 1) { err.textContent = "Escolha um setor."; err.style.display = ""; return; }
      err.style.display = "none"; btn.disabled = true; btn.textContent = "Ativando…";
      try { await completeRegistration(picked); }
      catch (ex) {
        err.textContent = ex.message || "Falha ao registrar.";
        err.style.display = ""; btn.disabled = false; btn.textContent = "Começar teste grátis";
      }
    });
  }

  /* ======================================================================
     FEED-CARD SELECTORS (read the shape build_feed already produces).
     ====================================================================== */
  const feed = (D) => (D && D.feed) || [];
  const lensSet = (c) => new Set(c.lenses || []);
  const hasLens = (c, ...ls) => { const s = lensSet(c); return ls.some((l) => s.has(l)); };
  const hasTopic = (c, ...ts) => { const s = new Set(c.topics || []); return ts.some((t) => s.has(t)); };
  const byDateThreat = (a, b) => (b.date || "").localeCompare(a.date || "") || (b.threat_score - a.threat_score);

  /* ======================================================================
     CORE COMPONENT — a cited feed card (the drill-down unit).
     Same .exc idiom as the admin exceptions feed: tier badge (color+glyph+word),
     entity, date, linkified narrative, inference label when derived, and the
     clickable citation footer. Clicking opens the shared drawer.
     ====================================================================== */
  // Industry slug -> pt-BR label (screen-agnostic: the standalone screens don't load
  // industries.js, so the change panel carries its own labels).
  const INDUSTRY_LABELS = {
    acquiring: "Adquirência", fintech: "Fintechs", banking: "Bancos", insurance: "Seguros",
    "investment-banking": "Banco de investimento", consorcio: "Consórcios",
    "asset-management": "Gestão de ativos", "wealth-management": "Wealth",
    "real-estate-funds": "FIIs", "agri-funds": "FIAGRO", "private-markets": "Private markets",
    "financial-data-analytics": "Dados & analytics", advisory: "Advisory", crypto: "Cripto",
    betting: "Apostas",
  };
  const indLabel = (s) => INDUSTRY_LABELS[s] || s;

  /* ADR 009 §5 — "Mudança regulatória" panel: the change intelligence carried on a
     regulatory card (Phase A change list + §3 rated record). Blast radius + difficulty
     are color+glyph+WORD (never color alone) and labeled inference; the change text +
     effective date are sourced. Empty string for a card with no change intel. */
  function _blastBadge(b) {
    const m = { market: ["crit", "▲", "amplo"], sector: ["high", "◆", "setorial"],
                narrow: ["med", "●", "restrito"] };
    const [cls, g, word] = m[b.band] || ["low", "▬", b.band || "—"];
    return `<span class="badge badge--${cls}"><span class="g" aria-hidden="true">${g}</span>` +
      `alcance ${word}${b.n_entities != null ? " · " + b.n_entities + " entid." : ""}</span>`;
  }
  function _diffBadge(d) {
    const m = { high: ["crit", "▲", "alta"], medium: ["high", "◆", "média"], low: ["low", "▬", "baixa"] };
    const [cls, g, word] = m[d.band] || ["low", "▬", d.band || "—"];
    return `<span class="badge badge--${cls}"><span class="g" aria-hidden="true">${g}</span>dificuldade ${word}</span>`;
  }
  function hasChange(c) { return !!(c && (c.change_record || c.n_changes || (c.changes || []).length)); }
  function changePanel(c) {
    if (!hasChange(c)) return "";
    const rec = c.change_record;
    const changes = c.changes || [];
    const list = changes.length
      ? `<ul class="chg-list">${changes.slice(0, 6).map((ch) =>
          `<li>${esc(ch.verb || "")} ${esc((ch.targets || []).map((t) => t.label).join(" e "))}` +
          `${(ch.articles || []).length ? ` <span class="cvsub">(${esc((ch.articles || []).join("; "))})</span>` : ""}</li>`).join("")}</ul>`
      : "";
    // Show the PRECISE affected cohort (#70) — `industries` is recall-first for scoping.
    const inds = (c.affected_industries && c.affected_industries.length ? c.affected_industries
      : (rec && rec.affected_industries)) || c.industries || [];
    const chips = inds.length
      ? `<div class="chiprow">${inds.map((s) => `<span class="chip">${esc(indLabel(s))}</span>`).join("")}</div>` : "";
    let rated = "";
    if (rec) {
      const eff = rec.effective_date ? `<span class="badge badge--ghost">vigência ${esc(U.fmtDate(rec.effective_date))}</span>`
        : (c.days_to_deadline != null ? `<span class="badge badge--ghost">${c.days_to_deadline}d p/ vigência</span>` : "");
      const drivers = (rec.difficulty && rec.difficulty.drivers || []).length
        ? `<div class="cvsub">Fatores: ${(rec.difficulty.drivers).map(esc).join(" · ")}</div>` : "";
      const surfaces = (rec.affected_surfaces || []).length
        ? `<div class="cvsub">Superfícies: ${rec.affected_surfaces.map(esc).join(" · ")}</div>` : "";
      rated = `<div class="chg-rated">
        <div class="chiprow">${_blastBadge(rec.blast_radius || {})} ${_diffBadge(rec.difficulty || {})} ${eff}
          <span class="badge badge--infer">inferência</span></div>
        ${rec.change ? `<div class="chg-line"><b>Mudança:</b> ${esc(rec.change)}</div>` : ""}
        ${rec.impact ? `<div class="chg-line"><b>Impacto:</b> ${esc(rec.impact)}</div>` : ""}
        ${rec.action_required ? `<div class="chg-line"><b>Ação:</b> ${esc(rec.action_required)}</div>` : ""}
        ${surfaces}${drivers}</div>`;
    }
    const n = c.n_changes || changes.length || 0;
    return `<details class="fold chg"><summary><span class="tw" aria-hidden="true">▸</span> Mudança regulatória` +
      `<span class="demoted">— ${n} alteração(ões)${rec ? " · impacto avaliado (inferência)" : ""}</span></summary>` +
      `<div class="chg-bd">${chips}${list}${rated}</div></details>`;
  }

  function cardHTML(c, i) {
    const model = U.citationModel(c);
    const infer = c.is_inference
      ? ' <span class="badge badge--infer">inferência</span>' : "";
    const noSrc = model.entries.length ? "" :
      ' <span class="badge badge--crit"><span class="g" aria-hidden="true">⚠</span>sem fonte</span>';
    const footer = model.entries.length ? U.citationFooter(model)
      : `<div class="cites"><span class="cite-grp"><span class="src">Sem fonte pública clicável</span>
         — apenas id de sinal interno (${esc((c.source_ids || [])[0] || "—")})</span></div>`;
    return `<div class="exc" data-i="${i}" role="button" tabindex="0" aria-label="Abrir detalhe">
      <div class="exc__row1">
        ${U.tierBadge(c.threat_score)}
        <span class="exc__ent">${esc(c.entity_label || c.subject_label || c.entity || "—")}</span>
        <span class="exc__date">${esc(U.fmtDate(c.date))}</span>
        <span class="spacer" style="flex:1"></span>${infer}${noSrc}
      </div>
      <div class="exc__body">${U.linkifyNarrative(c.narrative, model)}</div>
      ${footer}
      ${changePanel(c)}
    </div>`;
  }
  // Render a list of cards into `el`, wiring the drawer. `empty` = honest empty state.
  function renderCards(el, cards, emptyState) {
    if (!cards.length) { el.innerHTML = U.empty(emptyState.t, emptyState.d, emptyState.ico || "○"); return; }
    el.innerHTML = cards.map((c, i) => cardHTML(c, i)).join("");
    const open = (i) => openCard(cards[i]);
    el.querySelectorAll(".exc[data-i]").forEach((row) => {
      const i = +row.dataset.i;
      row.addEventListener("click", (e) => {
        if (e.target.closest("a") || e.target.closest(".chg")) return;  // link / change panel
        open(i);
      });
      row.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(i); } });
    });
  }

  /* ---- shared drawer (card + entity drill-down) ------------------------ */
  let DATA_REF = {};
  function setData(d) { DATA_REF = d || {}; }
  function attrsOf(eid) { return ((DATA_REF.entity_attrs || {})[eid]) || {}; }
  function entityRec(eid) { return (DATA_REF.entities || []).find((e) => e.entity === eid) || null; }

  function openCard(c) {
    const a = attrsOf(c.entity);
    const model = U.citationModel(c);
    const rec = entityRec(c.entity);
    const spark = rec && rec.timeline && rec.timeline.length
      ? `<div class="chartcanvas" style="margin:var(--s2) 0">${U.sparkline(
          rec.timeline.map((t) => t.max_score), { w: 460, h: 40, stroke: "var(--cat-steel)", label: "ameaça no tempo" })}</div>` : "";
    const ms = rec && rec.market_share_pct != null
      ? `${rec.market_share_pct}% <span class="cvsub">(IF.data)</span>`
      : `<span class="badge badge--infer">sem IF.data</span>`;
    document.getElementById("drTitle").textContent = c.entity_label || c.subject_label || "Detalhe";
    document.getElementById("drBody").innerHTML = `
      <div class="exc__row1" style="gap:var(--s2)">${U.tierBadge(c.threat_score)}
        ${c.is_inference ? '<span class="badge badge--infer">inferência</span>' : ""}
        <span class="exc__date">${esc(U.fmtDate(c.date))}</span></div>
      <div class="callout" style="margin-top:var(--s3)">${U.linkifyNarrative(c.narrative, model)}
        ${model.entries.length ? U.citationFooter(model)
          : `<div class="cites"><span class="cite-grp"><span class="src">Sem fonte pública clicável</span></span></div>`}</div>
      ${changePanel(c)}
      ${spark}
      <dl class="kv">
        <dt>Entidade</dt><dd>${esc(a.display_name || c.entity_label || c.entity || "—")}</dd>
        <dt>Indústrias</dt><dd>${esc((a.industries || rec && rec.industries || []).join(", ") || "—")}</dd>
        <dt>Ticker</dt><dd>${esc(a.ticker || "—")}</dd>
        <dt>Controle</dt><dd>${esc(a.ownership || "—")}</dd>
        <dt>Market share</dt><dd>${ms}</dd>
        <dt>Momentum</dt><dd>${rec ? rec.momentum : 0} <span class="cvsub">(contagem ponderada de expansão)</span></dd>
        <dt>Lentes</dt><dd>${esc((c.lenses || []).join(", ") || "—")}</dd>
      </dl>`;
    openDrawer();
  }
  function openDrawer() {
    const d = document.getElementById("drawer"); if (!d) return;
    d.classList.add("open"); d.setAttribute("aria-hidden", "false");
    document.getElementById("scrim").classList.add("open");
  }
  function closeDrawer() {
    const d = document.getElementById("drawer"); if (!d) return;
    d.classList.remove("open"); d.setAttribute("aria-hidden", "true");
    document.getElementById("scrim").classList.remove("open");
  }
  function wireDrawer() {
    const c = document.getElementById("drClose"), s = document.getElementById("scrim");
    if (c) c.onclick = closeDrawer;
    if (s) s.onclick = closeDrawer;
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
  }

  /* ======================================================================
     PANEL — KPI tiles (slim). tiles = [[label, value], ...]
     ====================================================================== */
  function renderKpis(el, tiles) {
    el.innerHTML = tiles.map(([l, n]) =>
      `<div class="tile tile--slim"><div class="tile__n num">${n == null ? "—" : n}</div>
       <div class="tile__l">${esc(l)}</div></div>`).join("");
  }

  /* ======================================================================
     PANEL — Comparar (closed-roster multi-entity threat × momentum).
     Ideal for a bounded roster (acquirers, insurers, gestoras). One aligned row
     per entity: peak-threat tier (color+glyph+word), a threat-over-time sparkline,
     a momentum bar (weighted expansion COUNT — never a 0–1 index), and market
     share ONLY when a sourced IF.data figure exists (else an "sem IF.data" infer
     chip — never an invented %). Small-multiples, not an overlaid spaghetti line,
     so it degrades honestly when a series is sparse.
     ====================================================================== */
  function renderComparar(el, D, opts) {
    opts = opts || {};
    let ents = (D.entities || []).slice();
    if (opts.filter) ents = ents.filter(opts.filter);
    ents.sort((a, b) => (b.peak_score || 0) - (a.peak_score || 0) || (b.momentum || 0) - (a.momentum || 0));
    ents = ents.slice(0, opts.limit || 12);
    if (!ents.length) { el.innerHTML = U.empty("Sem roster", "Nenhuma entidade escopada neste contexto.", "○"); return; }
    const maxMom = Math.max(1, ...ents.map((e) => e.momentum || 0));
    el.innerHTML = `<div class="covstrip">` + ents.map((e) => {
      const t = U.tierOf(e.peak_score);
      const series = (e.timeline || []).map((d) => d.max_score);
      const momPct = Math.round(((e.momentum || 0) / maxMom) * 100);
      const ms = (e.market_share_pct == null)
        ? `<span class="badge badge--infer">sem IF.data</span>`
        : `<span class="cvsub num">${e.market_share_pct}%</span>`;
      return `<div class="covrow" data-ent="${esc(e.entity)}" role="button" tabindex="0"
          style="grid-template-columns:160px 1fr 96px 92px;cursor:pointer">
        <div><div class="cvname">${esc(e.label)}</div>
          <div class="cvsub">${e.total || 0} sinais · mom ${e.momentum || 0}</div></div>
        <div class="cvspark chartcanvas">${U.sparkline(series, { w: 240, h: 26,
          stroke: "var(--cat-steel)", label: e.label + " ameaça no tempo" })}</div>
        <div class="cvflag">${U.tierBadge(e.peak_score)}</div>
        <div style="min-width:0">
          <div style="height:8px;border-radius:999px;background:var(--surface-3);overflow:hidden">
            <span style="display:block;height:100%;width:${momPct}%;background:var(--cat-teal)"></span></div>
          <div class="cvsub" style="text-align:right">${ms}</div></div>
      </div>`;
    }).join("") + `</div>
      <div class="legend">
        <span class="sw"><span class="dot" style="background:var(--cat-steel)"></span>ameaça (pico) no tempo</span>
        <span class="sw"><span class="dot" style="background:var(--cat-teal)"></span>momentum de expansão (contagem)</span>
        <span class="sw">market share só quando há IF.data — nunca estimado</span>
      </div>`;
    // Row -> open the entity's most recent card (drill to source).
    el.querySelectorAll(".covrow[data-ent]").forEach((row) => {
      const ent = row.dataset.ent;
      const go = () => {
        const c = feed(D).filter((x) => x.entity === ent).sort(byDateThreat)[0];
        if (c) openCard(c);
      };
      row.addEventListener("click", go);
      row.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    });
  }

  /* ======================================================================
     PANEL — Regulatório (rules feed). Filters the scoped feed to regulatory
     cards (lens regulatory/dou or topic regulação). `sources` narrows to a lens
     subset (e.g. DOU-only for SUSEP/PREVIC/CADE). Optional thin-coverage caveat.
     ====================================================================== */
  function renderRegulatorio(el, D, opts) {
    opts = opts || {};
    const all = feed(D).filter((c) =>
      (opts.lenses ? hasLens(c, ...opts.lenses) : hasLens(c, "regulatory", "dou")) || (!opts.lenses && hasTopic(c, "regulacao")));
    all.sort(byDateThreat);
    const caveat = opts.caveat && all.length < (opts.caveatBelow || 4)
      ? `<div class="callout callout--warn" style="margin-bottom:var(--s3)">${esc(opts.caveat)}</div>` : "";
    if (!all.length) {
      el.innerHTML = caveat + U.empty("Sem sinais regulatórios", opts.emptyD || "Nenhum normativo no feed escopado nesta janela.", "○");
      return;
    }
    // ADR 009 §5 — "Regulatório / Mudanças" facet: pull just the cards carrying a
    // regulatory CHANGE (Phase A list or the §3 rated record). Off by default.
    const nChg = all.filter(hasChange).length;
    const bar = nChg ? `<div class="filterbar" style="margin-bottom:var(--s3)">
        <label class="cvsub"><input type="checkbox" id="__regChg"> só mudanças
          <span class="badge badge--ghost">${nChg}</span></label></div>` : "";
    const wrap = document.createElement("div");
    el.innerHTML = caveat + bar; el.appendChild(wrap);
    const draw = (only) => renderCards(wrap, only ? all.filter(hasChange) : all,
      { t: "Sem mudanças regulatórias", d: "Nenhum normativo com alteração enumerada na janela." });
    draw(false);
    const cb = el.querySelector("#__regChg");
    if (cb) cb.addEventListener("change", () => draw(cb.checked));
  }

  /* ======================================================================
     PANEL — Calendário regulatório (deadlines). Cards carrying a deadline /
     days_to_deadline, soonest first, with an urgency badge (color+glyph+word).
     Reg-lifecycle stage shown when present. Cited like every card.
     ====================================================================== */
  function urgency(days) {
    if (days == null) return { cls: "low", g: "▬", label: "sem prazo" };
    if (days <= 7) return { cls: "crit", g: "▲", label: `${days}d` };
    if (days <= 30) return { cls: "high", g: "◆", label: `${days}d` };
    if (days <= 90) return { cls: "med", g: "●", label: `${days}d` };
    return { cls: "low", g: "▬", label: `${days}d` };
  }
  function renderCalendar(el, D) {
    let cards = feed(D).filter((c) => c.deadline || c.days_to_deadline != null);
    cards.sort((a, b) => (a.days_to_deadline == null ? 1e9 : a.days_to_deadline) - (b.days_to_deadline == null ? 1e9 : b.days_to_deadline));
    if (!cards.length) { el.innerHTML = U.empty("Sem prazos abertos", "Nenhum normativo com prazo em vigor na janela.", "○"); return; }
    el.innerHTML = cards.map((c, i) => {
      const u = urgency(c.days_to_deadline);
      const model = U.citationModel(c);
      const stage = c.current_stage ? `<span class="badge badge--ghost">${esc(String(c.current_stage).replace(/_/g, " "))}</span>` : "";
      return `<div class="qrow" data-i="${i}" role="button" tabindex="0">
        <div class="qrow__type"><span class="badge badge--${u.cls}"><span class="g" aria-hidden="true">${u.g}</span>${u.label}</span></div>
        <div class="qrow__main">
          <div class="qrow__title">${esc(c.subject_label || c.entity_label || "Regulatório")}</div>
          <div class="qrow__meta">${stage}${c.deadline ? `<span>prazo ${esc(U.fmtDate(c.deadline))}</span>` : ""}
            ${model.entries.length ? "" : '<span class="badge badge--crit"><span class="g" aria-hidden="true">⚠</span>sem fonte</span>'}</div>
        </div>
        <div class="qrow__actions"><span class="cvsub">abrir →</span></div>
      </div>`;
    }).join("");
    el.querySelectorAll(".qrow[data-i]").forEach((row) => {
      const i = +row.dataset.i;
      row.addEventListener("click", () => openCard(cards[i]));
      row.addEventListener("keydown", (e) => { if (e.key === "Enter") openCard(cards[i]); });
    });
  }

  /* ======================================================================
     PANEL — Novos entrantes + formation-velocity funnel.
     The discovery moat: authorization/licensing (entrants), participation (pix),
     and public/securities offerings (ofertas). Funnel numbers are CITED COUNTS of
     real filings in the window — explicitly labelled "contagem citada", NEVER a
     market-size estimate (which we don't have and won't fabricate).
     ====================================================================== */
  function renderEntrantsFunnel(el, D) {
    const cnt = (ls) => feed(D).filter((c) => hasLens(c, ...ls)).length;
    const stages = [
      { l: "Autorização", n: cnt(["entrants"]), sub: "licenciamento (BCB)" },
      { l: "Participação", n: cnt(["pix"]), sub: "atividade Pix / DICT" },
      { l: "Oferta", n: cnt(["ofertas", "funds"]), sub: "oferta / registro CVM" },
    ];
    el.innerHTML = `<div style="display:flex;align-items:stretch;gap:var(--s2);flex-wrap:wrap">` +
      stages.map((s, i) => `
        <div class="tile" style="flex:1;min-width:120px">
          <div class="tile__n num">${s.n}</div>
          <div class="tile__l">${esc(s.l)}</div>
          <div class="cvsub" style="margin-top:2px">${esc(s.sub)}</div>
        </div>${i < stages.length - 1 ? '<div style="align-self:center;color:var(--muted);font-size:20px" aria-hidden="true">→</div>' : ""}`).join("") +
      `</div><div class="legend"><span class="sw"><span class="badge badge--infer">contagem citada</span>
        filings reais na janela — não é tamanho de mercado</span></div>`;
  }
  function renderEntrants(el, D) {
    let cards = feed(D).filter((c) => hasLens(c, "entrants", "ofertas", "funds"));
    cards.sort(byDateThreat);
    renderCards(el, cards, { t: "Sem novos entrantes", d: "Nenhuma autorização, oferta ou registro de fundo na janela.", ico: "○" });
  }

  /* ======================================================================
     PANEL — Risco (BCB complaints ranking + entity-tagged distress).
     Complaints ranking is an OFFICIAL cited BCB figure (rank + index). Distress
     carries its evidence grade (regulatory vs news rumor) explicitly — a rumor is
     labelled a rumor, never presented as a filed fact.
     ====================================================================== */
  function renderRisco(el, D) {
    const rep = (D.reputation || []).slice().sort((a, b) => (b.index || 0) - (a.index || 0));
    const dist = (D.distress || []).slice().sort((a, b) => (b.last_seen || "").localeCompare(a.last_seen || ""));
    if (!rep.length && !dist.length) {
      el.innerHTML = U.empty("Sem sinais de risco", "Sem ranking de reclamações ou eventos de estresse para o roster escopado.", "✓");
      return;
    }
    let html = "";
    if (rep.length) {
      html += `<div class="panel__hd" style="border-top:0"><h2>Reclamações · ranking BCB</h2>
        <span class="spacer"></span><span class="hd-note">pior índice primeiro · fonte oficial</span></div>`;
      html += rep.map((r) => `<div class="qrow">
        <div class="qrow__type"><span class="heat ${r.index >= 15 ? "t5" : r.index >= 10 ? "t4" : r.index >= 6 ? "t3" : "t2"}">#${esc(r.rank)}</span></div>
        <div class="qrow__main">
          <div class="qrow__title"><b>${esc(r.company || r.entity)}</b> — índice ${esc(r.index)}
            <span class="badge badge--ghost">${esc(r.category || "")}</span></div>
          <div class="qrow__meta">${esc(r.period || "")} · ${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener">BCB ↗</a>` : "sem link"}</div>
        </div></div>`).join("");
    }
    if (dist.length) {
      html += `<div class="panel__hd"><h2>Estresse corporativo</h2><span class="spacer"></span>
        <span class="hd-note">RJ / falência · grau de evidência explícito</span></div>`;
      html += dist.map((r) => {
        const graded = r.confidence === "regulatory";
        const badge = graded
          ? `<span class="badge badge--crit"><span class="g" aria-hidden="true">▲</span>fato (regulatório)</span>`
          : `<span class="badge badge--warn"><span class="g" aria-hidden="true">◷</span>rumor (notícias)</span>`;
        return `<div class="qrow">
          <div class="qrow__type">${badge}</div>
          <div class="qrow__main">
            <div class="qrow__title"><b>${esc(labelOf(r.entity))}</b> — ${esc(r.label || r.kind)}</div>
            <div class="qrow__meta">${esc(r.latest_title || "")} · ${esc(U.fmtDate(r.last_seen))} · ${r.mentions || 0} menções
              ${r.latest_url ? ` · <a href="${esc(r.latest_url)}" target="_blank" rel="noopener">fonte ↗</a>` : ""}</div>
          </div></div>`;
      }).join("");
    }
    el.innerHTML = html;
  }

  /* ======================================================================
     PANEL — Substituição por Pix. Pix-lens cards framed as substitution
     pressure on card/acquiring rails. Cited cards; honest empty state.
     ====================================================================== */
  function renderPix(el, D) {
    let cards = feed(D).filter((c) => hasLens(c, "pix") || hasTopic(c, "pagamentos"));
    cards.sort(byDateThreat);
    const note = cards.length
      ? `<div class="callout" style="margin-bottom:var(--s3)">Sinais de Pix (DICT, novas modalidades) lidos como
         <b>pressão de substituição</b> sobre trilhos de cartão. Cada cartão traz sua fonte.</div>` : "";
    if (!cards.length) { el.innerHTML = U.empty("Sem sinais de Pix", "Nenhuma atividade de Pix na janela escopada.", "○"); return; }
    const wrap = document.createElement("div");
    el.innerHTML = note; el.appendChild(wrap);
    renderCards(wrap, cards, { t: "Sem sinais de Pix", d: "" });
  }

  /* ======================================================================
     PANEL — Fundos (velocity). CVM class/fund registrations + informe diário +
     FII/FIAGRO + ofertas. Net-new class-registration COUNT is shown as a cited
     share-of-shelf PROXY (dashed infer label) — never an AUM estimate unless a
     sourced IF.data/informe figure exists.
     ====================================================================== */
  function renderFundos(el, D) {
    let cards = feed(D).filter((c) => hasLens(c, "funds", "inf_diario", "ofertas") || hasTopic(c, "fundos"));
    cards.sort(byDateThreat);
    const regCount = feed(D).filter((c) => hasLens(c, "funds")).length;
    const offCount = feed(D).filter((c) => hasLens(c, "ofertas")).length;
    const strip = `<div style="display:flex;gap:var(--s2);flex-wrap:wrap;margin-bottom:var(--s3)">
      <div class="tile" style="flex:1;min-width:150px"><div class="tile__n num">${regCount}</div>
        <div class="tile__l">Registros de classe/fundo</div>
        <div class="cvsub" style="margin-top:2px"><span class="badge badge--infer">proxy de share-of-shelf</span> contagem citada</div></div>
      <div class="tile" style="flex:1;min-width:150px"><div class="tile__n num">${offCount}</div>
        <div class="tile__l">Ofertas em pipeline (CVM)</div>
        <div class="cvsub" style="margin-top:2px">contagem citada</div></div>
    </div>`;
    if (!cards.length) { el.innerHTML = strip + U.empty("Sem movimentação de fundos", "Nenhum registro/oferta na janela.", "○"); return; }
    const wrap = document.createElement("div");
    el.innerHTML = strip; el.appendChild(wrap);
    renderCards(wrap, cards, { t: "Sem movimentação de fundos", d: "" });
  }

  /* ======================================================================
     PANEL — Crença / SWOT competitive thesis (per entity).
     Renders ONLY vetted/active beliefs from the scoped belief store (swot +
     tows/porter curated). Precision-first: nothing un-vetted shows as a fact.
     Slow-cadence contexts (insurers, wealth) reward this accumulated belief.
     ====================================================================== */
  const DIM_PT = { S: "Forças", W: "Fraquezas", O: "Oportunidades", T: "Ameaças" };
  function labelOf(eid) {
    const r = entityRec(eid); if (r) return r.label;
    const a = attrsOf(eid); return a.display_name || eid;
  }
  function renderSWOT(el, D, opts) {
    opts = opts || {};
    const swot = D.swot || {};
    const ents = Object.keys(swot);
    if (!ents.length) {
      el.innerHTML = U.empty("Sem tese acumulada", "Nenhuma crença vetada para o roster escopado ainda — cadência lenta, base em formação.", "○");
      return;
    }
    // entity selector
    const sel = opts.entity && swot[opts.entity] ? opts.entity : ents[0];
    const picker = `<div class="filterbar" style="margin-bottom:var(--s3)">
      <label class="cvsub" for="__swotEnt">Entidade</label>
      <select id="__swotEnt" class="field">${ents.map((e) =>
        `<option value="${esc(e)}" ${e === sel ? "selected" : ""}>${esc(swot[e].label || labelOf(e))}</option>`).join("")}</select></div>`;
    el.innerHTML = picker + `<div id="__swotBody"></div>`;
    const draw = (eid) => {
      const s = swot[eid] || {};
      const dims = s.dimensions || {};
      const counts = s.counts || {};
      const cols = ["S", "W", "O", "T"].map((k) => {
        const bullets = ((dims[k] || {}).bullets || dims[k] || []).filter ? (dims[k].bullets || dims[k] || []) : [];
        const active = (Array.isArray(bullets) ? bullets : []).filter((b) => (b.status || "active") === "active");
        const items = active.length
          ? active.map((b) => `<li>${esc(b.text)} ${b.confidence != null ? U.heat(b.confidence) : ""}</li>`).join("")
          : `<li class="cvsub">${counts[k] ? counts[k] + " crença(s) vetada(s) — detalhe no war room" : "sem crença vetada"}</li>`;
        return `<div class="panel" style="box-shadow:none">
          <div class="panel__hd"><h2>${DIM_PT[k]}</h2><span class="count ${counts[k] ? "" : "count--zero"}">${counts[k] || 0}</span></div>
          <div class="panel__bd"><ul style="margin:0;padding-left:18px;font-size:var(--fs-sm)">${items}</ul></div></div>`;
      }).join("");
      // TOWS/Porter thesis strip
      const tows = (D.tows || {})[eid] || [];
      const porter = (D.porter || {})[eid] || [];
      const thesis = (tows.length || porter.length)
        ? `<div class="callout" style="margin-top:var(--s3)"><b>Tese competitiva</b>
            <ul style="margin:6px 0 0;padding-left:18px;font-size:var(--fs-sm)">
            ${tows.map((b) => `<li><span class="badge badge--ghost">TOWS ${esc(b.dimension)}</span> ${esc(b.text)}</li>`).join("")}
            ${porter.map((b) => `<li><span class="badge badge--ghost">Porter ${esc(b.dimension)}</span> ${esc(b.text)}</li>`).join("")}</ul></div>`
        : "";
      document.getElementById("__swotBody").innerHTML =
        `<div class="grid" style="grid-template-columns:repeat(2,1fr)">${cols}</div>${thesis}`;
    };
    draw(sel);
    const selEl = document.getElementById("__swotEnt");
    if (selEl) selEl.onchange = () => draw(selEl.value);
  }

  /* ======================================================================
     PANEL — Mapa (buyer hero, honest). Ranked bar of entities by peak threat,
     tier-encoded (color+glyph+word). Not a degenerate scatter — a legible rank
     that stays informative when the roster is small.
     ====================================================================== */
  function renderMapa(el, D, opts) {
    opts = opts || {};
    let ents = (D.entities || []).slice().sort((a, b) => (b.peak_score || 0) - (a.peak_score || 0)).slice(0, opts.limit || 12);
    if (!ents.length) { el.innerHTML = U.empty("Sem entidades", "Nenhuma entidade no feed escopado.", "○"); return; }
    el.innerHTML = ents.map((e) => {
      const t = U.tierOf(e.peak_score);
      const pct = Math.round((e.peak_score || 0) * 100);
      const varname = t.key === "crit" ? "t-crit" : t.key === "high" ? "t-high" : t.key === "med" ? "t-med" : "t-low";
      const extra = opts.momentum
        ? `<span class="cvsub num">mom ${e.momentum || 0}</span>`
        : (e.market_share_pct == null ? `<span class="badge badge--infer">sem IF.data</span>` : `<span class="cvsub num">${e.market_share_pct}%</span>`);
      return `<div class="covrow" style="grid-template-columns:160px 1fr 96px">
        <div class="cvname">${esc(e.label)} ${U.tierBadge(e.peak_score)}</div>
        <div class="cvspark"><div style="height:8px;border-radius:999px;background:var(--surface-3);overflow:hidden">
          <span style="display:block;height:100%;width:${pct}%;background:var(--${varname})"></span></div></div>
        <div class="cvflag">${extra}</div>
      </div>`;
    }).join("");
  }

  /* ======================================================================
     PANEL — Mapa de posição competitiva (XY quadrant): AMEAÇA (pico, eixo Y)
     × SINAIS DE EXPANSÃO (momentum = contagem ponderada de movimentos, eixo X).
     The hero of a per-industry tab: one dot per scoped entity, tier-colored,
     placed on two axes we already compute honestly — peak threat (0..1) and the
     weighted expansion COUNT (never a market-size estimate). Four quadrants read
     the roster at a glance: Ofensivos (alta ameaça + expansão), Consolidados
     (ameaça sem movimento), Desafiantes (expansão sem ameaça ainda), Latentes.
     Degrades honestly: <1 entity -> empty state; a single dot still places.
     Clicking a dot drills to that entity's most recent cited card.
     ====================================================================== */
  // Data-sufficiency gate for the 2-D position map. A scatter is only legible when
  // BOTH axes disperse — enough plotted competitors, at least two showing expansion
  // (else the X axis collapses to a vertical strip at 0), and a threat SPAN (else Y
  // collapses to a horizontal line). This is the regression-validity precondition
  // (each marginal variance must be > 0) — NOT a correlation/R² test, which would
  // wrongly reject a good quadrant whose points spread into all four corners. When
  // it fails (typically thin early-corpus data), the hero is withdrawn rather than
  // shown as a misleading 1-D strip; it auto-returns as the data fills in.
  const QUAD_MIN_N = 3, QUAD_MIN_EXPANDERS = 2, QUAD_MIN_THREAT_RANGE = 0.12;
  function quadStats(pts) {
    const n = pts.length;
    const moms = pts.map((p) => p.mom), thrs = pts.map((p) => p.thr);
    const std = (a) => { if (!a.length) return 0;
      const m = a.reduce((s, x) => s + x, 0) / a.length;
      return Math.sqrt(a.reduce((s, x) => s + (x - m) * (x - m), 0) / a.length); };
    return {
      n, expanders: moms.filter((m) => m > 0).length,
      threatRange: n ? Math.max(...thrs) - Math.min(...thrs) : 0,
      momStd: std(moms), threatStd: std(thrs),
    };
  }
  function quadrantViable(pts) {
    const s = quadStats(pts);
    if (s.n < QUAD_MIN_N) return { ok: false, reason: "poucos concorrentes com sinal", stats: s };
    if (s.expanders < QUAD_MIN_EXPANDERS)
      return { ok: false, reason: "sem variação de expansão (eixo X colapsa)", stats: s };
    if (s.threatRange < QUAD_MIN_THREAT_RANGE)
      return { ok: false, reason: "sem dispersão de ameaça (eixo Y colapsa)", stats: s };
    return { ok: true, stats: s };
  }

  function renderQuadrant(el, D, opts) {
    opts = opts || {};
    let ents = (D.entities || []).slice()
      .map((e) => ({ e, mom: Number(e.momentum) || 0, thr: Number(e.peak_score) || 0,
        total: Number(e.total) || 0 }))
      // rank by relevance so a crowded roster keeps the signal-bearing dots.
      .sort((a, b) => (b.thr - a.thr) || (b.mom - a.mom))
      .slice(0, opts.limit || 16);
    // Withdraw the hero when the scatter would be degenerate. The entities still
    // appear in every panel below; only the position MAP is suppressed. Hiding the
    // whole band (not a stub) keeps the layout clean; it un-hides when data returns.
    const band = el.closest ? el.closest(".band") : null;
    if (ents.length && !quadrantViable(ents).ok) {
      if (band) { band.style.display = "none"; band.dataset.quadHidden = "1"; }
      el.innerHTML = "";
      return;
    }
    if (band && band.dataset.quadHidden) { band.style.display = ""; delete band.dataset.quadHidden; }
    if (!ents.length) {
      el.innerHTML = U.empty("Sem mapa de posição",
        "Nenhuma entidade escopada com sinal neste setor na janela.", "○");
      return;
    }
    const W = 820, H = 460, ML = 52, MR = 20, MT = 26, MB = 40;
    const pW = W - ML - MR, pH = H - MT - MB;
    const maxMom = Math.max(3, ...ents.map((p) => p.mom));
    const maxTot = Math.max(1, ...ents.map((p) => p.total));
    // split lines: threat at the MÉDIO/ALTO seam (0.5); expansion at the roster median.
    const moms = ents.map((p) => p.mom).sort((a, b) => a - b);
    const median = moms.length % 2 ? moms[(moms.length - 1) / 2]
      : (moms[moms.length / 2 - 1] + moms[moms.length / 2]) / 2;
    const xSplitV = Math.max(1, median);
    const ySplitV = 0.5;
    const X = (m) => ML + (m / maxMom) * pW;
    const Y = (t) => MT + (1 - t) * pH;
    const xS = X(xSplitV), yS = Y(ySplitV);
    const quadTxt = (x, y, anchor, txt) =>
      `<text x="${x}" y="${y}" text-anchor="${anchor}" fill="var(--muted)"
        font-size="11" font-weight="700" letter-spacing=".4" opacity=".72">${esc(txt)}</text>`;
    // axis ticks: threat 0/.25/.5/.75/1, expansion 0..maxMom in ~4 steps.
    let grid = "";
    [0, 0.25, 0.5, 0.75, 1].forEach((t) => {
      const y = Y(t);
      grid += `<line x1="${ML}" y1="${y.toFixed(1)}" x2="${ML + pW}" y2="${y.toFixed(1)}"
        stroke="var(--border)" stroke-width="1" opacity=".55"/>
        <text x="${ML - 8}" y="${(y + 3).toFixed(1)}" text-anchor="end" fill="var(--muted)"
          font-size="10">${t.toFixed(2)}</text>`;
    });
    const xTicks = Math.min(maxMom, 6);
    for (let i = 0; i <= xTicks; i++) {
      const v = Math.round((maxMom / xTicks) * i);
      const x = X(v);
      grid += `<text x="${x.toFixed(1)}" y="${MT + pH + 15}" text-anchor="middle"
        fill="var(--muted)" font-size="10">${v}</text>`;
    }
    // dots + labels; label anchors flip near the right edge to avoid clipping.
    const dots = ents.map((p, i) => {
      const t = U.tierOf(p.thr);
      const cx = X(p.mom), cy = Y(p.thr);
      const r = 5 + Math.sqrt(p.total / maxTot) * 7;
      const flip = cx > ML + pW * 0.8;
      const lx = flip ? cx - r - 5 : cx + r + 5;
      const anc = flip ? "end" : "start";
      return `<g class="quad-pt" data-ent="${esc(p.e.entity)}" role="button" tabindex="0"
          aria-label="${esc(p.e.label)}: ameaça ${p.thr.toFixed(2)}, expansão ${p.mom}">
        <title>${esc(p.e.label)} — ameaça ${p.thr.toFixed(2)} · expansão ${p.mom} · ${p.total} sinais</title>
        <circle cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="${r.toFixed(1)}"
          fill="var(--t-${t.key}-fill)" stroke="var(--t-${t.key})" stroke-width="1.75"/>
        <text x="${lx.toFixed(1)}" y="${(cy + 3.5).toFixed(1)}" text-anchor="${anc}"
          fill="var(--ink-2)" font-size="11" font-weight="600">${esc(p.e.label)}</text>
      </g>`;
    }).join("");
    el.innerHTML = `<div class="chartcanvas quadwrap">
      <svg viewBox="0 0 ${W} ${H}" role="img" width="100%"
        aria-label="Mapa de posição: ameaça (pico) por sinais de expansão (momentum)">
        <rect x="${ML}" y="${MT}" width="${pW}" height="${pH}" fill="none" stroke="var(--border-2)" stroke-width="1"/>
        ${grid}
        <line x1="${xS.toFixed(1)}" y1="${MT}" x2="${xS.toFixed(1)}" y2="${MT + pH}"
          stroke="var(--border-2)" stroke-width="1.25" stroke-dasharray="4 4"/>
        <line x1="${ML}" y1="${yS.toFixed(1)}" x2="${ML + pW}" y2="${yS.toFixed(1)}"
          stroke="var(--border-2)" stroke-width="1.25" stroke-dasharray="4 4"/>
        ${quadTxt(ML + pW - 6, MT + 14, "end", "OFENSIVOS")}
        ${quadTxt(ML + 6, MT + 14, "start", "CONSOLIDADOS")}
        ${quadTxt(ML + pW - 6, MT + pH - 8, "end", "DESAFIANTES")}
        ${quadTxt(ML + 6, MT + pH - 8, "start", "LATENTES")}
        <text x="${ML + pW / 2}" y="${H - 4}" text-anchor="middle" fill="var(--ink-2)"
          font-size="11" font-weight="700">SINAIS DE EXPANSÃO  (momentum — contagem ponderada) →</text>
        <text x="14" y="${MT + pH / 2}" text-anchor="middle" fill="var(--ink-2)" font-size="11"
          font-weight="700" transform="rotate(-90 14 ${MT + pH / 2})">AMEAÇA (pico) →</text>
        ${dots}
      </svg></div>
      <div class="legend">
        <span class="sw"><span class="dot" style="background:var(--t-crit)"></span>ameaça = pico no tempo (0–1)</span>
        <span class="sw"><span class="dot" style="background:var(--cat-teal)"></span>expansão = contagem ponderada de movimentos</span>
        <span class="sw">tamanho do ponto = total de sinais · clique para a fonte</span>
      </div>`;
    el.querySelectorAll(".quad-pt[data-ent]").forEach((g) => {
      const ent = g.dataset.ent;
      const go = () => {
        const c = feed(D).filter((x) => x.entity === ent).sort(byDateThreat)[0];
        if (c) openCard(c);
      };
      g.addEventListener("click", go);
      g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
    });
  }

  /* ---- as-of stamp ----------------------------------------------------- */
  function setAsOf(D) {
    const el = document.getElementById("asof"); if (!el) return;
    const parts = [];
    if (D.as_of) parts.push(`dados de ${U.fmtDate(D.as_of)}`);
    if (D.feed) parts.push(`${D.feed.length} narrativas`);
    if (D.dates) parts.push(`janela de ${D.dates.length} dias`);
    if (D.scoped_modules) parts.push(`escopo: ${D.scoped_modules.join(", ")}`);
    if (D.tier === "entry") parts.push("tier entrada");
    el.textContent = parts.join(" · ");
  }

  /* ---- ADR 016/024 Entry plan on /exec -----------------------------------------
     An Entry tenant (paid Entry or its trial) is served the Entry slice by /api/feed: its one
     sector, shallow public-filing cards only, no officer dashboards. /exec is where Entry
     lands (pricing CTA, post-registration re-auth — the Cognito callback, and the one page
     outside the shared basic-auth), so it renders this Entry board instead of the officer
     suite, with the officer views stated as "Disponível no SaaS" + an assinar CTA. */
  function isEntryTier(D) { return !!D && D.tier === "entry"; }
  function mountEntryBoard(D) {
    const $ = (id) => document.getElementById(id);
    ["askPanel", "recsBand", "officerTabs", "viewTabs"].forEach((id) => { const el = $(id); if (el) el.hidden = true; });
    const sel = $("sectorSel");
    if (sel && sel.parentElement) sel.parentElement.hidden = true;
    const mods = D.scoped_modules || [];
    const sector = mods.map(indLabel).join(", ") || "—";
    if ($("pgTitle")) $("pgTitle").textContent = "Plano Entry — " + sector;
    if ($("pgLede")) $("pgLede").textContent = "Fatos públicos do seu setor — registros, normativos, "
      + "ofertas — cada um com a fonte clicável. Os painéis executivos fazem parte do plano SaaS.";
    const board = $("board");
    if (!board) return;
    const cards = feed(D).slice().sort(byDateThreat);
    const ctas = mods.filter((m) => bandOf(m)).map((m) =>
      `<a class="btn btn--primary" href="${esc(upgradeHref(bandOf(m), m, D.upgrade))}">Assinar SaaS · ${esc(indLabel(m))}</a>`).join(" ");
    board.innerHTML = `<div class="tiles" id="entryKpis" style="margin-bottom:var(--s4)"></div>
      <section class="band"><div class="band-hd"><span class="overline">Sinais do setor · ${esc(sector)}</span>
        <span class="rule"></span><span class="badge badge--ghost">Entry</span></div>
        <div class="panel"><div class="panel__hd"><h2>Fatos públicos recentes</h2><span class="count">${cards.length}</span></div>
          <div class="panel__bd panel__bd--flush panel__bd--scroll" id="entryCards"></div></div></section>
      <section class="band" id="entrySaas"><div class="band-hd"><span class="overline">Painéis executivos</span>
        <span class="rule"></span><span class="badge badge--ghost">SaaS</span></div>
        <div class="panel"><div class="panel__bd"><div class="empty"><div class="em-ico" aria-hidden="true">◐</div>
          <div class="em-t">Disponível no SaaS</div>
          <div class="em-d">Painéis por diretoria (CSO, CRO, CCO, CPO), estresse e recuperação judicial, movimentos
            de capital, reputação, demonstrações financeiras, frameworks por entidade e Pergunte à Onça fazem
            parte do plano SaaS, licenciado por setor.</div>
          <div style="margin-top:var(--s3)">${ctas}</div>
          <div class="em-d" style="margin-top:var(--s2)"><a href="/pricing.html">Ver planos</a> ·
            <a href="/docs/">Guia do plano Entry</a></div></div></div></div></section>`;
    const k = D.kpis || {};
    renderKpis($("entryKpis"), [["Sinais hoje", k.narratives_latest], ["Alertas hoje", k.alerts_latest],
      ["Entidades", k.entities_tracked], ["Fontes distintas", k.sources]]);
    setData(D);
    renderCards($("entryCards"), cards, { t: "Sem sinais na janela",
      d: "Nenhum fato público do seu setor na janela atual." });
  }

  /* ---- fixed segment pages (/fintech, /seguros, /adquirencia, /wealth) ----------
     Each page frames ONE industry. Unlicensed ⇒ say so, point to the tenant's own sectors, and
     offer that segment's subscription — never another sector's data under this framing. */
  function segmentGateHTML(D, acc) {
    const home = isEntryTier(D) ? "/exec" : "/app";
    const band = bandOf(acc.slug);
    const mine = (acc.licensed || []).map((s) =>
      `<a href="${home}#${esc(encodeURIComponent(s))}">${esc(indLabel(s))}</a>`).join(", ");
    return `<div class="empty"><div class="em-ico" aria-hidden="true">⦸</div>
      <div class="em-t">${esc(indLabel(acc.slug))} não está na sua licença</div>
      <div class="em-d">Esta página mostra apenas ${esc(indLabel(acc.slug))}; dados de outros setores não
        aparecem sob este enquadramento.${mine ? " Seus setores: " + mine + "." : ""}</div>
      <div style="margin-top:var(--s3)"><a class="btn" href="${home}">Abrir meus setores</a>
        ${band ? `<a class="btn btn--primary" href="${esc(upgradeHref(band, acc.slug, D.upgrade))}">Assinar ${esc(indLabel(acc.slug))}</a>` : ""}</div></div>`;
  }
  function mountSegmentGate(D, acc) {
    const main = document.querySelector("main") || document.body;
    const prev = document.getElementById("segmentGate"); if (prev) prev.remove();
    main.querySelectorAll("section.band, #kpis").forEach((el) => { el.style.display = "none"; });
    const box = document.createElement("div");
    box.className = "panel"; box.id = "segmentGate";
    box.innerHTML = `<div class="panel__bd">${segmentGateHTML(D, acc)}</div>`;
    const head = main.querySelector(".pagehead");
    if (head && head.nextSibling) main.insertBefore(box, head.nextSibling); else main.appendChild(box);
  }

  /* ======================================================================
     SHARED BOOT — a SaaS context screen. Handles theme, drawer, the Cognito
     redirect callback, the auth box, the scoped-feed load, and honest gating
     (login / no-access / error) BEFORE any panel renders. The screen supplies
     only { ctxLabel, render(D) } — its lead order lives in its own render().
     ====================================================================== */
  async function bootSaaS(cfg) {
    U.initTheme();
    wireDrawer();
    await handleAuthCallback();
    const themeBtn = document.getElementById("themeBtn");
    if (themeBtn) themeBtn.addEventListener("click", () => { U.toggleTheme(); if (global.DATA) cfg.render(global.DATA); });
    const leadEls = () => Array.from(document.querySelectorAll(".panel--lead .panel__bd"));
    async function load() {
      await ensureSession();
      renderAuthBox("authBox");
      const first = leadEls()[0] || document.querySelector(".panel__bd");
      const res = await loadScopedFeed({});
      if (res.needAuth) { if (first) mountGate(first, "needAuth", cfg.ctxLabel); return; }
      if (res.noAccess) { if (first) mountGate(first, "noAccess", cfg.ctxLabel, res); return; }
      if (res.error) { if (first) mountGate(first, res.error, cfg.ctxLabel); return; }
      let D = res.data;
      // cfg.segment: a fixed segment page — slice to that industry within the entitlement
      // (industries.js), or gate it when the tenant doesn't license it.
      const Ind = global.OncaIndustries;
      if (cfg.segment) {
        if (!Ind) { if (first) mountGate(first, "segmento indisponível", cfg.ctxLabel); return; }  // fail closed
        const acc = Ind.segmentAccess(D, cfg.segment);
        if (!acc.ok) { mountSegmentGate(D, acc); return; }
        D = Ind.sliceToIndustry(D, acc.slug);
      }
      global.DATA = D; setData(global.DATA); setAsOf(global.DATA);
      cfg.render(global.DATA);
    }
    // Re-render on login/logout without a full reload where possible.
    global.__oncaReload = load;
    await load();
  }

  global.OncaCtx = {
    // auth
    login, logout, isLoggedIn, getIdToken, renderAuthBox, handleAuthCallback,
    ensureSession, clearUserData, decodeJwt, readCachedFeed: readUserFeed,
    bootSaaS,
    // feed
    loadScopedFeed, mountGate, setData, indLabel, upgradeHref, billingBannerHTML, bandOf, SAAS_BANDS, comboOf, combosOf,
    isEntryTier, mountEntryBoard, segmentGateHTML,
    // drawer
    wireDrawer, openCard, closeDrawer,
    // panels
    renderKpis, renderComparar, renderRegulatorio, renderCalendar,
    renderEntrants, renderEntrantsFunnel, renderRisco, renderPix,
    renderFundos, renderSWOT, renderMapa, renderQuadrant, quadrantViable, quadStats,
    // selectors / util
    feed, hasLens, hasTopic, setAsOf, labelOf,
  };
})(window);
