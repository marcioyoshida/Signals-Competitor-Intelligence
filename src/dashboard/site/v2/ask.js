/* ============================================================================
   Onça Design System v2 — "Perguntar à Onça" grounded Q&A bar (ADR 010).
   ----------------------------------------------------------------------------
   The shared, restyled port of the live site's #askPanel onto the v2 system.
   FRONT-END ONLY: it POSTs same-origin `/api/ask` with `Authorization: Bearer
   <id_token>`; the server (agent_ask.py) scopes the grounding pool to the
   verified tenant's licensed modules (fail-closed). No client-side data gating
   ever happens here — a fintech tenant's answer can't cite wealth-only data
   because the SERVER strips it, not this file.

   ONE module, six screens:
     - The five buyer screens already carry Cognito PKCE auth in context.js
       (OncaCtx). They pass it in as `opts.auth` so the SAME session token is
       reused — no second login path.
     - Admin loads app.js only (no context.js). It uses this module's built-in
       auth, which shares the SAME sessionStorage token key + client, so a
       session established anywhere in the family is honoured here too.
     - The Entry tier (newentry) is a static, login-less slice with NO /api/ask
       entitlement — it renders an honest "disponível no plano SaaS" state, never
       a broken control (matches the live Entry Portal, which ships "no agent").

   Honest states, never a fabricated answer:
     not-logged-in -> login prompt · entry -> upgrade note · loading -> spinner
     server decline -> shown plainly · 401 -> drop token + re-prompt · error ->
     honest failure (the static harness 404s /api/ask; that degrades here).
   ============================================================================ */
(function (global) {
  "use strict";
  const U = global.OncaUI;
  const esc = U.esc;

  /* ---- built-in auth (mirrors context.js EXACTLY: same domain/client/token
     key, so the session is shared across the whole v2 family). Used only when a
     host screen does not inject its own auth (i.e. admin). ------------------ */
  const AUTH = {
    domain: "https://onca-668449743071.auth.us-east-1.amazoncognito.com",
    clientId: "7qlquhh56o06tp9bo8gp77p385",
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
  async function login() {
    const verifier = randomPkceVerifier();
    sessionStorage.setItem(VERIFIER_KEY, verifier);
    const challenge = await pkceChallenge(verifier);
    const params = new URLSearchParams({
      client_id: AUTH.clientId, response_type: "code", scope: "openid email",
      redirect_uri: AUTH.redirectUri, code_challenge: challenge, code_challenge_method: "S256",
    });
    location.href = `${AUTH.domain}/login?${params.toString()}`;
  }
  function logout() {
    try { sessionStorage.removeItem(TOKEN_KEY); } catch (e) {}
    const params = new URLSearchParams({ client_id: AUTH.clientId, logout_uri: AUTH.redirectUri });
    location.href = `${AUTH.domain}/logout?${params.toString()}`;
  }
  async function exchangeCodeForToken(code) {
    const verifier = sessionStorage.getItem(VERIFIER_KEY);
    sessionStorage.removeItem(VERIFIER_KEY);
    if (!verifier) return false;
    try {
      const body = new URLSearchParams({
        grant_type: "authorization_code", client_id: AUTH.clientId,
        code, redirect_uri: AUTH.redirectUri, code_verifier: verifier,
      });
      const r = await fetch(`${AUTH.domain}/oauth2/token`, {
        method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" },
        body: body.toString(),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const j = await r.json();
      if (!j.id_token) throw new Error("sem id_token");
      sessionStorage.setItem(TOKEN_KEY, j.id_token);
      return true;
    } catch (e) { console.error("Onça auth: troca de código falhou", e); return false; }
  }
  async function handleAuthCallback() {
    const url = new URL(location.href);
    const code = url.searchParams.get("code");
    if (!code) return;
    await exchangeCodeForToken(code);
    url.searchParams.delete("code"); url.searchParams.delete("state");
    history.replaceState({}, "", url.pathname + (url.search || "") + url.hash);
  }
  function accountEmail(auth) {
    const t = (auth.getIdToken && auth.getIdToken()) || null;
    const p = t ? decodeJwt(t) : null;
    return (p && p.email) || "conta";
  }
  const builtinAuth = { login, logout, isLoggedIn, getIdToken, handleAuthCallback };

  const DEFAULT_EXAMPLES = [
    "O que mudou recentemente no meu roster?",
    "Quais entidades estão em alerta esta semana?",
    "Quem está exposto à regra do DICT?",
    "Que novos entrantes apareceram na janela?",
  ];

  /* ---- answer rendering: inline [id] refs -> clickable superscripts, plus a
     v2 citation footer (source label + clickable refnum links). Reuses the
     .cites / .refnum idiom so it reads as one family with every card. ------- */
  function renderAnswer(box, res, opts) {
    box.hidden = false;
    if (res.refused || !res.answer) {
      box.className = "ask-ans ask-ans--refused";
      box.textContent = res.answer || "Não consegui responder.";
      return;
    }
    box.className = "ask-ans";
    const cites = res.citations || [];
    const byId = {};
    cites.forEach((c, i) => { byId[c.id] = { n: i + 1, entity: c.entity }; });
    // Inline [id] -> superscript [n], clickable when it maps to a cited entity.
    let html = esc(res.answer).replace(/\[([A-Za-z0-9:_\-]+)\]/g, (m, id) => {
      const c = byId[id];
      if (!c) return "";
      return `<sup class="ask-aref" data-entity="${esc(c.entity || "")}" title="${esc(id)}">[${c.n}]</sup>`;
    });
    if (cites.length) {
      html += `<div class="cites ask-cites">` + cites.map((c, i) => {
        const label = c.kb ? "base de conhecimento" : (c.entity_label || c.entity || c.id);
        // Source URLs (drill-down): pull http links off the card's own citations.
        const srcs = (c.sources || []).filter((s) => s && /^https?:\/\//i.test(s.url || ""));
        const refs = srcs.map((s) =>
          `<a class="refnum" href="${esc(s.url)}" target="_blank" rel="noopener"
             title="${esc(U.sourceLabel(s.url))}">↗</a>`).join(" ");
        const ent = c.kb ? "" : (c.entity || "");
        return `<span class="cite-grp"><span class="ask-cite src" data-entity="${esc(ent)}"
          role="button" tabindex="0">[${i + 1}] ${esc(label)}</span> ${refs}</span>`;
      }).join("") + `</div>`;
    } else {
      html += `<div class="cites"><span class="cite-grp"><span class="src">Sem fontes citadas</span>
        — resposta não fundamentada; trate com cautela.</span></div>`;
    }
    box.innerHTML = html;
    if (opts.onCite) {
      box.querySelectorAll("[data-entity]").forEach((el) => {
        const en = el.dataset.entity;
        if (!en) return;
        const go = () => opts.onCite(en);
        el.addEventListener("click", go);
        el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
      });
    }
  }

  /* ---- the mount ---------------------------------------------------------- */
  // OncaAsk.mount(bodyEl, opts)
  //   opts.auth        {login,logout,isLoggedIn,getIdToken,handleAuthCallback}
  //                    (buyer screens pass OncaCtx; admin omits -> built-in)
  //   opts.entry       true  -> honest "disponível no plano SaaS", no API call
  //   opts.apiUrl      default "" (same origin; CloudFront -> JWT HTTP API)
  //   opts.scope       optional () => ({entity,lens,date}) request scope hint
  //   opts.onCite      optional (entity) => void  (drill from a citation)
  //   opts.showAccount optional bool -> show connected-as/Sair inside the panel
  //   opts.examples    optional string[] of example questions
  async function mount(bodyEl, opts) {
    opts = opts || {};
    if (!bodyEl) return;
    const auth = opts.auth || builtinAuth;
    const apiUrl = opts.apiUrl || "";
    const examples = opts.examples || DEFAULT_EXAMPLES;

    // Entry tier: no /api/ask entitlement (login-less static slice). Show the
    // bar as an honest upgrade affordance, never a control that 404s on submit.
    if (opts.entry) {
      bodyEl.innerHTML = `
        <div class="askbar">
          <input class="field ask-input" type="search" disabled
            placeholder="Q&A fundamentado disponível no plano SaaS" aria-label="Perguntar à Onça" />
          <button class="btn" disabled>Perguntar</button>
        </div>
        <div class="ask-hint">
          <span class="badge badge--ghost"><span class="g" aria-hidden="true">◐</span>plano SaaS</span>
          <span>O assistente fundamentado (resposta só com fonte citada, escopada à sua licença)
            faz parte do plano SaaS. No tier de entrada, explore os painéis de novos entrantes e
            regulatório acima.</span>
        </div>`;
      return;
    }

    // If this screen owns auth (admin/built-in), consume the ?code= redirect.
    if (auth === builtinAuth && auth.handleAuthCallback) {
      try { await auth.handleAuthCallback(); } catch (e) {}
    }

    bodyEl.innerHTML = `
      <div class="askbar">
        <input class="field ask-input" type="search" id="__askInput"
          placeholder="Ex.: o que mudou esta semana? quem está exposto à regra do DICT?"
          aria-label="Perguntar à Onça" />
        <button class="btn btn--primary" id="__askBtn">Perguntar</button>
      </div>
      <div class="ask-ex" id="__askEx"></div>
      <div class="ask-hint" id="__askHint" hidden>
        <span class="badge badge--ghost"><span class="g" aria-hidden="true">◐</span>login</span>
        <span>Entre para perguntar — a resposta é escopada no servidor à sua licença.</span>
        <button class="btn btn--primary btn--sm" id="__askLogin">Entrar</button>
      </div>
      <div class="ask-acct" id="__askAcct" hidden></div>
      <div class="ask-ans" id="__askAns" hidden></div>`;

    const input = bodyEl.querySelector("#__askInput");
    const btn = bodyEl.querySelector("#__askBtn");
    const exBox = bodyEl.querySelector("#__askEx");
    const hint = bodyEl.querySelector("#__askHint");
    const acct = bodyEl.querySelector("#__askAcct");
    const ans = bodyEl.querySelector("#__askAns");

    // Example chips.
    exBox.innerHTML = examples.map((q) => `<span class="ask-chip" role="button" tabindex="0">${esc(q)}</span>`).join("");
    exBox.querySelectorAll(".ask-chip").forEach((c) => {
      const run = () => { input.value = c.textContent; ask(); };
      c.addEventListener("click", run);
      c.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); run(); } });
    });

    function syncLoginState() {
      const logged = auth.isLoggedIn();
      hint.hidden = logged;
      input.disabled = !logged;
      btn.disabled = !logged;
      input.placeholder = logged
        ? "Ex.: o que mudou esta semana? quem está exposto à regra do DICT?"
        : "Entre para perguntar à Onça";
      // Optional in-panel account line (admin, which has no header authBox).
      if (opts.showAccount && logged) {
        acct.hidden = false;
        acct.innerHTML = `<span class="sub">conectado como ${esc(accountEmail(auth))}</span>
          <button class="btn btn--sm" id="__askLogout">Sair</button>`;
        const lo = acct.querySelector("#__askLogout");
        if (lo && auth.logout) lo.onclick = auth.logout;
      } else {
        acct.hidden = true; acct.innerHTML = "";
      }
    }

    const loginBtn = bodyEl.querySelector("#__askLogin");
    if (loginBtn && auth.login) loginBtn.onclick = auth.login;

    let busy = false;
    async function ask() {
      if (busy) return;
      if (!auth.isLoggedIn()) { syncLoginState(); return; } // never call silently
      const q = (input.value || "").trim();
      if (!q) return;
      busy = true;
      const prev = btn.textContent;
      btn.disabled = true; btn.textContent = "Pensando…";
      ans.hidden = false; ans.className = "ask-ans";
      ans.innerHTML = `<span class="thinking">Consultando a base da Onça…</span>`;
      const scope = (opts.scope && opts.scope()) || {};
      try {
        const r = await fetch(`${apiUrl}/api/ask`, {
          method: "POST",
          headers: { "content-type": "application/json", "authorization": `Bearer ${auth.getIdToken()}` },
          body: JSON.stringify({ q, scope }),
        });
        if (r.status === 401) {
          try { sessionStorage.removeItem(TOKEN_KEY); } catch (e) {}
          syncLoginState();
          renderAnswer(ans, { refused: true, answer: "Sessão expirada. Entre novamente para perguntar." }, opts);
          return;
        }
        const j = await r.json().catch(() => null);
        if (r.ok && j) renderAnswer(ans, j, opts);
        else renderAnswer(ans, { refused: true, answer: "Não foi possível consultar a base agora. Tente novamente." }, opts);
      } catch (e) {
        renderAnswer(ans, { refused: true, answer: "Falha de rede ao consultar a base da Onça." }, opts);
      } finally {
        btn.disabled = false; btn.textContent = prev; busy = false;
      }
    }

    btn.onclick = ask;
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") ask(); });
    syncLoginState();

    // Expose a refresh hook so a host can re-sync after its own auth flow.
    bodyEl.__askSync = syncLoginState;
  }

  global.OncaAsk = { mount, auth: builtinAuth };
})(window);
