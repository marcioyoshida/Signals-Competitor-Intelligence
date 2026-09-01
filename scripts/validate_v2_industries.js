const fs = require("fs");
// --- minimal runtime stubs (validate MY engine + registry, not PR#66 panels) ---
const panelCalls = [];
const noop = (name) => (el, D, opts) => { panelCalls.push(name); if (el) el.__rendered = name; };
const els = {};
function fakeEl(){ return { innerHTML:"", textContent:"", appendChild(){}, __rendered:null }; }
global.window = global;
global.OncaUI = { esc: (s)=>String(s==null?"":s) };
global.OncaCtx = {
  renderComparar:noop("comparar"), renderRegulatorio:noop("regulatorio"),
  renderCalendar:noop("calendar"), renderEntrants:noop("entrants"),
  renderEntrantsFunnel:noop("entrantsFunnel"), renderRisco:noop("risco"),
  renderPix:noop("pix"), renderFundos:noop("fundos"), renderSWOT:noop("swot"),
  renderMapa:noop("mapa"), renderKpis:noop("kpis"), renderQuadrant:noop("quadrant"),
};
global.document = { getElementById:(id)=> (els[id] || (els[id]=fakeEl())) };
// load the file (IIFE attaches window.OncaIndustries)
eval(fs.readFileSync("src/dashboard/site/v2/industries.js","utf8"));
const Ind = global.OncaIndustries;

const PANEL_KINDS = new Set(["comparar","regulatorio","calendar","entrants","entrantsFunnel","risco","pix","fundos","swot","mapa"]);
const taxonomy = (fs.readFileSync("src/synth/entity_registry.py","utf8").match(/"([a-z-]+)": \{"display_name/g)||[]).map(x=>x.match(/"([a-z-]+)"/)[1]);
let fail = 0;

// 1) every FS taxonomy slug resolves to a config (bespoke or generic — never blank)
const bespoke = Object.keys(Ind.REGISTRY);
const missing = taxonomy.filter(s => !bespoke.includes(s));
console.log("taxonomy:", taxonomy.length, "| bespoke designs:", bespoke.length,
  "| without bespoke (use generic):", missing.join(",") || "(none)");

// 2) every lead kind in every config is a known panel
function checkLeads(cfg, name){
  const flat = [];
  (cfg.leads||[]).forEach(l => Array.isArray(l) ? l.forEach(x=>flat.push(x)) : flat.push(l));
  (cfg.fold||[]).forEach(x=>flat.push(x));
  flat.forEach(it => { if(!PANEL_KINDS.has(it.kind)){ console.error("BAD kind",name,it.kind); fail++; } });
}
bespoke.forEach(s => checkLeads(Ind.REGISTRY[s], s));
checkLeads(Ind.configFor("__unknown__"), "GENERIC");

// 3) renderDashboard runs for every industry (bespoke + generic) without throwing
const D = { kpis:{narratives_latest:7,alerts_latest:2,entities_tracked:12,sources:9},
  scoped_modules:["banking"], feed:[], industries:[] };
[...bespoke, "unknown-sector"].forEach(slug => {
  try { const cfg = Ind.renderDashboard(fakeEl(), D, slug);
    if(!cfg || !cfg.title){ console.error("no cfg for", slug); fail++; } }
  catch(e){ console.error("THREW for", slug, e.message); fail++; }
});

// 4) licensedIndustries reads scoped_modules; activeSlug fallback via first
const lic = Ind.licensedIndustries({scoped_modules:["wealth-management","banking"]});
if (JSON.stringify(lic)!==JSON.stringify(["wealth-management","banking"])){ console.error("licensed wrong",lic); fail++; }
// 5) per-industry slice: a card tagged only 'banking' must NOT appear under 'acquiring',
//    and an acquiring-tagged entity/card MUST. Filtering is within the entitlement.
const Dmix = {
  run_date: "2026-08-31",
  entity_attrs: { stone: {industries:["acquiring"]}, itau: {industries:["banking"]} },
  groups: {}, entities: [{entity:"stone"},{entity:"itau"}],
  feed: [
    { id:"c1", entity:"stone", industries:["acquiring"], date:"2026-08-31", is_alert:true, citations:[{url:"https://bcb.gov.br/x"}] },
    { id:"c2", entity:"itau", industries:["banking"], date:"2026-08-31", citations:[{url:"https://cvm.gov.br/y"}] },
    { id:"c3", entity:"stone", industries:["acquiring","fintech"], date:"2026-08-31", citations:[] },
  ],
  kpis:{sources:9},
};
const acq = Ind.sliceToIndustry(Dmix, "acquiring");
const bank = Ind.sliceToIndustry(Dmix, "banking");
const acqIds = acq.feed.map(c=>c.id).sort();
if (JSON.stringify(acqIds)!==JSON.stringify(["c1","c3"])){ console.error("acquiring slice wrong:",acqIds); fail++; }
if (bank.feed.map(c=>c.id).join()!=="c2"){ console.error("banking slice wrong:",bank.feed.map(c=>c.id)); fail++; }
if (acq.kpis.narratives_total!==2 || acq.kpis.narratives_latest!==2 || acq.kpis.alerts_latest!==1){ console.error("acq kpis wrong:",acq.kpis); fail++; }
if (acq.entities.map(e=>e.entity).join()!=="stone"){ console.error("acq entities wrong",acq.entities); fail++; }
if (JSON.stringify(acq.scoped_modules)!==JSON.stringify(["acquiring"])){ console.error("scoped_modules wrong"); fail++; }

console.log(fail===0 ? "VALIDATION OK — all industries render, all kinds valid, per-industry slice filters" : ("FAIL: "+fail));
process.exit(fail?1:0);
