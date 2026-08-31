/* ============================================================================
   Onça Design System v2 — shared runtime (buildless, no framework).
   Reusable across all six context views. Pure helpers + primitives only;
   each screen supplies its own render() and lead order.
   ============================================================================ */
(function (global) {
  "use strict";

  /* ---- escaping / formatting ------------------------------------------- */
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // ISO -> DD-MM-YYYY for display; values/keys stay ISO for sort/filter.
  function fmtDate(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ""));
    return m ? `${m[3]}-${m[2]}-${m[1]}` : String(iso || "");
  }

  /* ---- threat tier taxonomy (shared with the live site) ---------------- */
  const TIERS = [
    { min: 0.85, key: "crit", label: "CRÍTICO", g: "▲" },
    { min: 0.65, key: "high", label: "ALTO",    g: "◆" },
    { min: 0.40, key: "med",  label: "MÉDIO",   g: "●" },
    { min: 0.00, key: "low",  label: "BAIXO",   g: "▬" },
  ];
  const tierOf = (s) => TIERS.find((t) => (s ?? 0) >= t.min) || TIERS[TIERS.length - 1];
  // A tier badge always pairs color + glyph + word (never color alone).
  function tierBadge(score) {
    const t = tierOf(score);
    return `<span class="badge badge--${t.key}"><span class="g" aria-hidden="true">${t.g}</span>${t.label}</span>`;
  }

  /* ---- confidence heat (0..1 -> 5 ordinal tiers) ----------------------- */
  function heat(conf) {
    const c = Number(conf) || 0;
    const pct = Math.round(c * 100);
    const t = c >= 0.9 ? 5 : c >= 0.8 ? 4 : c >= 0.7 ? 3 : c >= 0.6 ? 2 : 1;
    return `<span class="heat t${t}" title="confiança ${pct}%">${pct}%</span>`;
  }

  /* ---- source labelling ------------------------------------------------ */
  const SOURCE_LABELS = {
    "bcb.gov.br": "BCB", "dados.cvm.gov.br": "CVM", "rad.cvm.gov.br": "CVM",
    "sec.gov": "SEC", "news.google.com": "Google Notícias",
    "valor.globo.com": "Valor Econômico", "moneytimes.com.br": "Money Times",
  };
  function hostOf(url) {
    try { return new URL(url).hostname.replace(/^www\./, ""); }
    catch (e) { return String(url || ""); }
  }
  const sourceLabel = (url) => SOURCE_LABELS[hostOf(url)] || hostOf(url);

  /* ---- citation model (proven footnote idiom from the live site) ------- */
  // Every synthesized claim shows its clickable source. Citations with no http
  // URL (internal signal ids) carry no link — that ABSENCE is itself the
  // exception signal the admin view surfaces, never hidden.
  function citationModel(f) {
    const strip = (u) => String(u || "").replace(/[)\].,;:!?]+$/, "");
    const isHttp = (u) => typeof u === "string" && /^https?:\/\//i.test(u);
    const entries = [];
    const numByUrl = new Map();
    const add = (url) => {
      const k = strip(url);
      if (numByUrl.has(k)) return numByUrl.get(k);
      const n = entries.length + 1;
      numByUrl.set(k, n);
      entries.push({ n, url, label: sourceLabel(url) });
      return n;
    };
    (f.citations || []).forEach((c) => { if (isHttp(c.url)) add(c.url); });
    return { entries, numByUrl, strip, add, isHttp };
  }
  const URL_RE = /https?:\/\/[^\s<>]+/g;
  function linkifyNarrative(text, model) {
    text = String(text || "");
    let out = "", last = 0, m;
    URL_RE.lastIndex = 0;
    while ((m = URL_RE.exec(text))) {
      out += esc(text.slice(last, m.index));
      let url = m[0], trail = "";
      const tm = url.match(/[)\].,;:!?]+$/);
      if (tm) { trail = tm[0]; url = url.slice(0, -trail.length); }
      const n = model.numByUrl.get(model.strip(url)) || model.add(url);
      out += `<sup class="sup"><a href="${esc(url)}" target="_blank" rel="noopener">[${n}]</a></sup>` + esc(trail);
      last = m.index + m[0].length;
    }
    return out + esc(text.slice(last));
  }
  function citationFooter(model) {
    if (!model.entries.length) return "";
    const groups = new Map();
    model.entries.forEach((e) => {
      let g = groups.get(e.label);
      if (!g) { g = { label: e.label, refs: [] }; groups.set(e.label, g); }
      g.refs.push(e);
    });
    return `<div class="cites">` + [...groups.values()].map((g) => {
      const refs = g.refs.map((e) => e.url
        ? `<a class="refnum" href="${esc(e.url)}" target="_blank" rel="noopener">[${e.n}]</a>`
        : `<span class="refnum dead">[${e.n}]</span>`).join(" ");
      return `<span class="cite-grp"><span class="src">${esc(g.label)}</span> ${refs}</span>`;
    }).join("") + `</div>`;
  }

  /* ---- inline SVG sparkline (hand-rolled idiom, no chart lib) ----------- */
  // Encodes trend by LINE SHAPE + baseline, not color alone. Degenerate
  // input (0 or 1 point, or all-equal) renders an honest flat baseline with a
  // label rather than a misleading spike.
  function sparkline(values, opts) {
    opts = opts || {};
    const w = opts.w || 120, h = opts.h || 24, pad = 2;
    // A fixed-height, width-filling sparkline: preserveAspectRatio="none" lets the
    // SVG stretch to its column while CSS pins the height; vector-effect keeps the
    // stroke crisp under that non-uniform scale.
    const par = opts.fill === false ? "" : ' preserveAspectRatio="none"';
    const vals = (values || []).map((v) => Number(v) || 0);
    if (vals.length < 2) {
      return `<svg viewBox="0 0 ${w} ${h}"${par} role="img" aria-label="sem série suficiente">
        <line x1="${pad}" y1="${h - pad}" x2="${w - pad}" y2="${h - pad}"
          stroke="var(--border-2)" stroke-width="1" stroke-dasharray="3 3" vector-effect="non-scaling-stroke"/></svg>`;
    }
    const max = Math.max(...vals), min = Math.min(...vals);
    const span = max - min || 1;
    const stepX = (w - pad * 2) / (vals.length - 1);
    const pts = vals.map((v, i) => {
      const x = pad + i * stepX;
      const y = (max === min)
        ? h - pad - 1
        : h - pad - ((v - min) / span) * (h - pad * 2);
      return [x, y];
    });
    const d = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
    const stroke = opts.stroke || "var(--cat-steel)";
    // baseline reference (zero) so a flat-low series reads as "quiet", not blank.
    const zeroY = (max === min) ? h - pad - 1 : h - pad - ((0 - min) / span) * (h - pad * 2);
    return `<svg viewBox="0 0 ${w} ${h}"${par} role="img" aria-label="${esc(opts.label || "série")}">
      <line x1="${pad}" y1="${zeroY.toFixed(1)}" x2="${w - pad}" y2="${zeroY.toFixed(1)}"
        stroke="var(--border)" stroke-width="1" vector-effect="non-scaling-stroke"/>
      <path d="${d}" fill="none" stroke="${stroke}" stroke-width="1.75"
        stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>
    </svg>`;
  }

  /* ---- theme -------------------------------------------------------------
     Persist per-user; default follows OS, falls back to dark (the war-room
     default of the live site). */
  const THEME_KEY = "onca.theme.v2";
  function initTheme() {
    let t = null;
    try { t = localStorage.getItem(THEME_KEY); } catch (e) {}
    if (!t) {
      const mq = global.matchMedia && global.matchMedia("(prefers-color-scheme: light)");
      t = mq && mq.matches ? "light" : "dark";
    }
    document.documentElement.setAttribute("data-theme", t);
    return t;
  }
  function toggleTheme() {
    const cur = document.documentElement.getAttribute("data-theme");
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
    return next;
  }

  /* ---- honest empty state --------------------------------------------- */
  function empty(title, detail, ico) {
    return `<div class="empty"><div class="em-ico" aria-hidden="true">${ico || "○"}</div>
      <div class="em-t">${esc(title)}</div>${detail ? `<div class="em-d">${esc(detail)}</div>` : ""}</div>`;
  }

  global.OncaUI = {
    esc, fmtDate, TIERS, tierOf, tierBadge, heat,
    hostOf, sourceLabel, citationModel, linkifyNarrative, citationFooter,
    sparkline, initTheme, toggleTheme, empty,
  };
})(window);
