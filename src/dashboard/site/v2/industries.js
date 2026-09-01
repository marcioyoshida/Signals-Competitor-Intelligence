/* ============================================================================
   Onça Design System v2 — INDUSTRY dashboard registry + profile-driven renderer.
   ----------------------------------------------------------------------------
   One buyer, one login, one screen — chosen by the Cognito profile. The router
   shell (/app/) loads the server-scoped feed (/api/feed, authorized by the JWT),
   reads the tenant's LICENSED industries from `scoped_modules`, and renders THIS
   registry's design for the active one. Every FS industry has a bespoke design =
   a page lede + a lead-panel order over the shared app.css component vocabulary
   (the same panels the four hand-built screens use). Adding an industry design is
   one entry here — no new HTML file.

   Honesty invariants are inherited from context.js (every panel is built by
   OncaCtx.render*): cited claims only, `inferência` badges, honest empty states,
   server-side scoping. This file NEVER filters a superset — it only chooses which
   already-scoped panels to lead with.
   ============================================================================ */
(function (global) {
  "use strict";
  const Ctx = global.OncaCtx, U = global.OncaUI;

  // kind -> the OncaCtx panel renderer. Every panel takes (el, D[, opts]).
  const PANEL = {
    comparar: Ctx.renderComparar, regulatorio: Ctx.renderRegulatorio,
    calendar: Ctx.renderCalendar, entrants: Ctx.renderEntrants,
    entrantsFunnel: Ctx.renderEntrantsFunnel, risco: Ctx.renderRisco,
    pix: Ctx.renderPix, fundos: Ctx.renderFundos, swot: Ctx.renderSWOT,
    mapa: Ctx.renderMapa,
  };
  const DEFAULT_KPIS = [
    ["Sinais hoje", "narratives_latest"], ["Alertas hoje", "alerts_latest"],
    ["Entidades", "entities_tracked"], ["Fontes distintas", "sources"],
  ];

  // A lead is {kind, title, note, opts, scroll} or a 2-tuple of them (a 2-col row).
  const L = (kind, title, note, extra) => Object.assign({ kind, title, note }, extra || {});

  // --- The per-industry designs (all FS taxonomy slugs) ---------------------
  const REGISTRY = {
    acquiring: {
      label: "Adquirência",
      title: "Adquirência — oligopólio de maquininhas",
      lede: "Roster fechado (Cielo, Getnet, Rede, Stone, PagBank, InfinitePay, Mercado Pago). " +
        "Comparar lidera; o risco ancora no ranking oficial de reclamações do BCB; a substituição por Pix fecha.",
      ask: ["Como está o momentum da Stone vs. Cielo?", "Quem lidera o ranking de reclamações do BCB?",
        "Onde o Pix está pressionando as maquininhas?"],
      kpis: [["Sinais hoje", "narratives_latest"], ["Alertas hoje", "alerts_latest"],
        ["Entidades no roster", "entities_tracked"], ["Fontes distintas", "sources"]],
      leads: [L("comparar", "Roster de adquirência", "pico de ameaça no tempo · momentum", { opts: { limit: 12 } }),
        [L("risco", "Risco", "ranking BCB + estresse", { scroll: true }),
         L("pix", "Substituição por Pix", "pressão sobre trilhos de cartão", { scroll: true })]],
      fold: [L("fundos", "Fundos e ofertas", "secundário na adquirência")],
    },
    fintech: {
      label: "Fintechs", title: "Fintechs — a fronteira que mais se move",
      lede: "O calendário regulatório lidera (o que muda e quando), o funil de novos entrantes " +
        "mede a pressão competitiva, e o estresse fecha.",
      ask: ["Que mudanças regulatórias afetam pagamentos este mês?", "Quantos novos entrantes na janela?",
        "Alguma fintech em estresse?"],
      leads: [L("calendar", "Calendário regulatório", "o que muda e quando"),
        [L("entrantsFunnel", "Funil de entrantes", "autorização → tração"),
         L("risco", "Estresse", "recuperação judicial / falência", { scroll: true })]],
      fold: [L("comparar", "Comparar", "roster de fintechs"), L("swot", "SWOT", "teses vetadas")],
    },
    insurance: {
      label: "Seguros", title: "Seguros — o regulador é o sinal",
      lede: "SUSEP e o DOU lideram (atos, normas, autorizações), Comparar posiciona as seguradoras, " +
        "e o SWOT sintetiza a tese.",
      ask: ["Que atos da SUSEP saíram na janela?", "Como se posicionam as maiores seguradoras?",
        "Qual a tese sobre o segmento?"],
      leads: [L("regulatorio", "SUSEP · DOU", "atos e normas do setor"),
        [L("comparar", "Comparar seguradoras", "ameaça × momentum", { opts: { limit: 12 } }),
         L("swot", "SWOT", "teses vetadas")]],
    },
    "wealth-management": {
      label: "Wealth", title: "Wealth — a velocidade dos fundos é a tese",
      lede: "Fundos lidera (velocidade de captação/fluxo), Comparar posiciona as gestoras, e o SWOT sintetiza.",
      ask: ["Qual gestora está captando mais rápido?", "Como se posicionam as casas de wealth?",
        "Qual a tese sobre o segmento?"],
      leads: [L("fundos", "Velocidade de fundos", "captação e fluxo"),
        [L("comparar", "Comparar gestoras", "ameaça × momentum", { opts: { limit: 12 } }),
         L("swot", "SWOT", "teses vetadas")]],
    },
    "asset-management": {
      label: "Gestão de ativos", title: "Gestão de ativos — captação e produto",
      lede: "Fundos lidera (novas classes, fluxo), Comparar posiciona as gestoras, e o SWOT sintetiza.",
      ask: ["Quais novas classes de fundos foram registradas?", "Como se posicionam as gestoras?"],
      leads: [L("fundos", "Fundos e captação", "novas classes · fluxo"),
        [L("comparar", "Comparar gestoras", "ameaça × momentum", { opts: { limit: 12 } }),
         L("swot", "SWOT", "teses vetadas")]],
    },
    banking: {
      label: "Bancos", title: "Bancos — o núcleo do sistema",
      lede: "Comparar posiciona o roster; o regulatório (BCB/CMN) e o risco (reclamações + estresse) ancoram.",
      ask: ["Como se posiciona o roster dos grandes bancos?", "Que normas do BCB saíram na janela?",
        "Quem lidera as reclamações?"],
      leads: [L("comparar", "Roster de bancos", "ameaça × momentum", { opts: { limit: 12 } }),
        [L("regulatorio", "BCB · CMN", "normas e resoluções"),
         L("risco", "Risco", "reclamações + estresse", { scroll: true })]],
      fold: [L("fundos", "Fundos e ofertas", "secundário"), L("swot", "SWOT", "teses vetadas")],
    },
    "investment-banking": {
      label: "Banco de investimento", title: "Banco de investimento — deals e mandato",
      lede: "Comparar posiciona as casas; o regulatório e o SWOT ancoram a leitura de mandato.",
      ask: ["Como se posicionam os bancos de investimento?", "Que teses sobre o segmento?"],
      leads: [L("comparar", "Comparar casas", "ameaça × momentum", { opts: { limit: 12 } }),
        [L("swot", "SWOT", "teses vetadas"), L("regulatorio", "Regulatório", "atos do setor")]],
    },
    "private-markets": {
      label: "Private markets", title: "Private markets — VC/PE e captação",
      lede: "O funil de novos entrantes e o roster lideram; o mapa posiciona o ecossistema.",
      ask: ["Que novos gestores/veículos surgiram?", "Como está o mapa do ecossistema?"],
      leads: [L("entrants", "Novos veículos e gestores", "autorização → tração"),
        [L("comparar", "Comparar", "ameaça × momentum", { opts: { limit: 12 } }),
         L("mapa", "Mapa", "posição no ecossistema")]],
    },
    "financial-data-analytics": {
      label: "Dados & analytics", title: "Dados & analytics — a infraestrutura de informação",
      lede: "Comparar posiciona os provedores; o mapa e o regulatório ancoram.",
      ask: ["Como se posicionam os provedores de dados?", "Que mudanças regulatórias afetam o segmento?"],
      leads: [L("comparar", "Comparar provedores", "ameaça × momentum", { opts: { limit: 12 } }),
        [L("mapa", "Mapa", "posição no ecossistema"), L("regulatorio", "Regulatório", "atos do setor")]],
    },
    advisory: {
      label: "Advisory", title: "Advisory — assessoria e M&A",
      lede: "Comparar posiciona as assessorias; o mapa e os novos entrantes ancoram.",
      ask: ["Como se posicionam as assessorias?", "Que novos players surgiram?"],
      leads: [L("comparar", "Comparar assessorias", "ameaça × momentum", { opts: { limit: 12 } }),
        [L("mapa", "Mapa", "posição no ecossistema"), L("entrants", "Novos players", "autorização → tração")]],
    },
    crypto: {
      label: "Cripto", title: "Cripto & ativos digitais — regra em formação",
      lede: "O regulatório lidera (a regra ainda se forma), o funil de entrantes e o estresse ancoram.",
      ask: ["Que mudanças regulatórias na cripto?", "Que novos players surgiram?"],
      leads: [L("regulatorio", "Regulatório", "a regra em formação"),
        [L("entrants", "Novos players", "autorização → tração"),
         L("risco", "Risco", "estresse e reclamações", { scroll: true })]],
    },
    consorcio: {
      label: "Consórcios", title: "Consórcios — administradoras e reclamações",
      lede: "Novos entrantes e o risco (reclamações do BCB) lideram; o regulatório ancora.",
      ask: ["Que novas administradoras de consórcio?", "Quem lidera as reclamações?"],
      leads: [L("entrants", "Novas administradoras", "autorização → tração"),
        [L("risco", "Risco", "reclamações + estresse", { scroll: true }),
         L("regulatorio", "Regulatório", "atos do setor")]],
    },
    betting: {
      label: "Apostas", title: "Apostas & iGaming — a regulação recente",
      lede: "O regulatório lidera (o setor acabou de ser regulado), os novos entrantes e o risco ancoram.",
      ask: ["Que atos regulatórios sobre apostas?", "Que operadores foram autorizados?"],
      leads: [L("regulatorio", "Regulatório", "regulação recente"),
        [L("entrants", "Operadores autorizados", "autorização → tração"),
         L("risco", "Risco", "reclamações + estresse", { scroll: true })]],
    },
    "real-estate-funds": {
      label: "FIIs", title: "Fundos imobiliários — captação e ofertas",
      lede: "Fundos lidera (captação, novas classes), Comparar posiciona, e o SWOT sintetiza.",
      ask: ["Quais FIIs captaram mais rápido?", "Que novas ofertas na janela?"],
      leads: [L("fundos", "Velocidade de fundos", "captação e ofertas"),
        [L("comparar", "Comparar", "ameaça × momentum", { opts: { limit: 12 } }),
         L("swot", "SWOT", "teses vetadas")]],
    },
    "agri-funds": {
      label: "FIAGRO", title: "Fundos do agro — a fronteira dos FIAGRO",
      lede: "Fundos lidera (captação, novas classes), Comparar posiciona, e o SWOT sintetiza.",
      ask: ["Quais FIAGRO captaram mais rápido?", "Que novas ofertas na janela?"],
      leads: [L("fundos", "Velocidade de fundos", "captação e ofertas"),
        [L("comparar", "Comparar", "ameaça × momentum", { opts: { limit: 12 } }),
         L("swot", "SWOT", "teses vetadas")]],
    },
  };

  // Generic fallback for any industry without a bespoke design (never blank).
  const GENERIC = {
    label: "Panorama", title: "Panorama do setor",
    lede: "Comparar posiciona as entidades; o regulatório e o risco ancoram.",
    ask: ["Como se posicionam as principais entidades?", "Que mudanças regulatórias na janela?"],
    leads: [L("comparar", "Comparar", "ameaça × momentum", { opts: { limit: 12 } }),
      [L("regulatorio", "Regulatório", "atos do setor"),
       L("risco", "Risco", "reclamações + estresse", { scroll: true })]],
    fold: [L("fundos", "Fundos e ofertas", "secundário"), L("swot", "SWOT", "teses vetadas")],
  };

  function configFor(slug) { return REGISTRY[slug] || GENERIC; }

  // --- Per-industry slice (within the ENTITLED feed) -------------------------
  // The server scopes /api/feed to the tenant's whole entitlement (all its licensed
  // industries). A per-industry TAB must then show only THAT industry's signals — else a
  // multi-industry tenant sees, e.g., banks under Adquirência. This filters the tenant's OWN
  // licensed data to the active industry (NOT a superset — ADR 002 is about the entitlement
  // boundary, which the server already enforced). Card `industries[]` (ADR-017 denorm) is
  // authoritative; entities fall back to their registry industries; group children of an
  // in-scope parent are included (ADR-017 tier-1 opt-in). KPIs are recomputed for the slice.
  const _norm = (x) => String(x == null ? "" : x).toLowerCase();
  function _host(url) { try { return new URL(url).host.replace(/^www\./, ""); } catch (e) { return ""; } }

  function sliceToIndustry(D, slug) {
    if (!D || !slug) return D;
    const attrs = D.entity_attrs || {};
    const scoped = new Set(Object.keys(attrs).filter(
      (e) => (attrs[e].industries || []).map(_norm).indexOf(slug) !== -1));
    const groups = D.groups || {};
    const stack = Array.from(scoped).filter((e) => groups[e]);
    while (stack.length) {
      (groups[stack.pop()] || []).forEach((ch) => {
        if (!scoped.has(ch)) { scoped.add(ch); if (groups[ch]) stack.push(ch); }
      });
    }
    const cardOK = (c) => {
      const inds = (c.industries || []).map(_norm);
      if (inds.length) return inds.indexOf(slug) !== -1;      // denorm is authoritative
      return [c.entity].concat(c.entities || []).some((e) => e && scoped.has(e));
    };
    const feed = (D.feed || []).filter(cardOK);
    const entities = (D.entities || []).filter((r) => scoped.has(r.entity));
    const latest = D.run_date;
    const latestItems = feed.filter((c) => c.date === latest);
    const srcs = new Set();
    feed.forEach((c) => (c.citations || []).forEach((cit) => {
      const h = _host(cit && (cit.url || cit.href || cit)); if (h) srcs.add(h);
    }));
    const byKey = (o) => {                    // dict keyed by entity -> keep scoped keys
      const r = {}; Object.keys(o || {}).forEach((k) => { if (scoped.has(k)) r[k] = o[k]; }); return r;
    };
    const byEnt = (arr, field) => (arr || []).filter((r) => scoped.has(r[field || "entity"]));
    return Object.assign({}, D, {
      feed, entities, entity_attrs: byKey(attrs), groups: byKey(groups),
      scoped_modules: [slug],
      // entity-keyed auxiliary stores the panels read (risco/swot/mapa) — same slice, so no
      // panel shows a cross-industry entity. Absent keys stay absent (honest empty states).
      reputation: byEnt(D.reputation), distress: byEnt(D.distress),
      financials: byEnt(D.financials, "entity_id"),
      swot: byKey(D.swot), tows: byKey(D.tows), porter: byKey(D.porter), bcg: byKey(D.bcg),
      kpis: {
        narratives_latest: latestItems.length,
        alerts_latest: latestItems.filter((c) => c.is_alert).length,
        entities_tracked: entities.length,
        sources: srcs.size || (D.kpis && D.kpis.sources) || 0,
        narratives_total: feed.length,
      },
    });
  }

  // The tenant's licensed industry slugs, from the SERVER-SCOPED feed (never derived
  // on the client from a superset): scoped_modules is authoritative; industries[] is a
  // labelled fallback.
  function licensedIndustries(D) {
    const mods = (D && D.scoped_modules) || [];
    if (mods.length) return mods.map((m) => String(m).toLowerCase());
    return ((D && D.industries) || []).map((i) => String(i.slug || i).toLowerCase());
  }
  function labelFor(slug) { return (REGISTRY[slug] && REGISTRY[slug].label) || slug; }

  // --- The render engine: a config + scoped feed -> the screen ---------------
  let _uid = 0;
  function panelHTML(item, tasks, D) {
    const id = "p" + ++_uid;
    tasks.push([PANEL[item.kind], id, D, item.opts]);
    const bd = `panel__bd${item.scroll ? " panel__bd--flush panel__bd--scroll" : ""}`;
    return `<div class="panel panel--lead"><div class="panel__hd"><h2>${U.esc(item.title)}</h2>` +
      `<span class="spacer"></span><span class="hd-note">${U.esc(item.note || "")}</span></div>` +
      `<div class="${bd}" id="${id}"></div></div>`;
  }
  function bandHTML(lead, tasks, D) {
    if (Array.isArray(lead)) {
      const cols = lead.map((it) => panelHTML(it, tasks, D)).join("");
      const over = lead.map((it) => it.title).join(" · ");
      return `<section class="band"><div class="band-hd"><span class="overline">${U.esc(over)}</span>` +
        `<span class="rule"></span></div><div class="grid lead-3" style="grid-template-columns:1fr 1fr">${cols}</div></section>`;
    }
    return `<section class="band"><div class="band-hd"><span class="overline">${U.esc(lead.title)}</span>` +
      `<span class="rule"></span></div>${panelHTML(lead, tasks, D)}</section>`;
  }

  function renderDashboard(root, D, slug) {
    const cfg = configFor(slug);
    D = sliceToIndustry(D, slug);   // per-tab data = ONLY the active industry (within entitlement)
    const set = (id, txt) => { const e = document.getElementById(id); if (e) e.textContent = txt; };
    set("pageTitle", cfg.title);
    set("pageLede", cfg.lede);
    const kel = document.getElementById("kpis");
    if (kel) Ctx.renderKpis(kel, (cfg.kpis || DEFAULT_KPIS).map(([l, k]) => [l, (D.kpis || {})[k]]));

    const tasks = [];
    let html = (cfg.leads || []).map((lead) => bandHTML(lead, tasks, D)).join("");
    if (cfg.fold && cfg.fold.length) {
      const foldBodies = cfg.fold.map((it) => {
        const id = "p" + ++_uid; tasks.push([PANEL[it.kind], id, D, it.opts]);
        return `<details class="fold"><summary><span class="tw" aria-hidden="true">▸</span> ${U.esc(it.title)}` +
          `<span class="demoted">— ${U.esc(it.note || "secundário")}</span></summary>` +
          `<div class="fold__bd" id="${id}"></div></details>`;
      }).join("");
      html += `<section class="band">${foldBodies}</section>`;
    }
    root.innerHTML = html;
    // Panels render AFTER the nodes are in the DOM (some measure/adjust on mount).
    tasks.forEach(([fn, id, data, opts]) => {
      const el = document.getElementById(id);
      if (el && fn) { try { fn(el, data, opts || {}); } catch (e) { console.error("panel", id, e); } }
    });
    return cfg;
  }

  global.OncaIndustries = {
    REGISTRY, configFor, licensedIndustries, labelFor, renderDashboard, sliceToIndustry,
  };
})(window);
