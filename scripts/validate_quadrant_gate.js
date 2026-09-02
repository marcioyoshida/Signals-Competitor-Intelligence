/* Headless check of the AMEAÇA × EXPANSÃO position-map data-sufficiency gate
   (OncaCtx.quadrantViable). A 2-D scatter is only shown when BOTH axes disperse:
   >= 3 plotted competitors, >= 2 with expansion > 0 (else X collapses), and a
   threat SPAN >= 0.12 (else Y collapses). This is the regression-validity
   precondition (marginal variance > 0), not a correlation/R² test. */
const fs = require("fs");
global.window = global;
global.location = { search: "", hash: "", href: "http://x/", pathname: "/", origin: "http://x" };
global.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
global.localStorage = { getItem: () => null, setItem() {} };
global.matchMedia = () => ({ matches: false });
global.document = { getElementById: () => null, addEventListener() {},
  querySelector: () => null, querySelectorAll: () => ({ forEach() {} }) };
eval(fs.readFileSync("src/dashboard/site/v2/app.js", "utf8"));
eval(fs.readFileSync("src/dashboard/site/v2/context.js", "utf8"));
const Ctx = global.OncaCtx;

let fail = 0;
const pts = (arr) => arr.map(([mom, thr]) => ({ mom, thr, total: 1 }));
function expect(name, got, want) {
  if (got !== want) { console.error(`FAIL ${name}: got ${got} want ${want}`); fail++; }
}

// --- healthy: spread on both axes -> SHOWN ---
expect("healthy spread", Ctx.quadrantViable(pts([[38, 0.93], [15, 0.79], [1, 0.6], [0, 0.44]])).ok, true);
expect("thin-but-spread n=3", Ctx.quadrantViable(pts([[34, 0.85], [4, 0.59], [1, 0.5]])).ok, true);
expect("two expanders only", Ctx.quadrantViable(pts([[2, 0.64], [1, 0.61], [0, 0.44], [0, 0.44]])).ok, true);

// --- degenerate: X axis collapses (0 or 1 expander) -> WITHDRAWN ---
expect("all expansion zero (Dados/FIIs)", Ctx.quadrantViable(pts([[0, 0.62], [0, 0.44], [0, 0.44], [0, 0.44], [0, 0.44]])).ok, false);
expect("single expander (Apostas/insurance)", Ctx.quadrantViable(pts([[1, 0.61], [0, 0.44], [0, 0.44], [0, 0.44], [0, 0.44], [0, 0.44]])).ok, false);

// --- degenerate: Y axis collapses (no threat span) -> WITHDRAWN ---
expect("threat flat (Cripto)", Ctx.quadrantViable(pts([[5, 0.45], [3, 0.44], [2, 0.44], [1, 0.44]])).ok, false);
expect("single stacked point (FIIs)", Ctx.quadrantViable(pts([[0, 0.44], [0, 0.44], [0, 0.44]])).ok, false);

// --- degenerate: too few points -> WITHDRAWN ---
expect("too few (n<3)", Ctx.quadrantViable(pts([[10, 0.9], [1, 0.5]])).ok, false);
expect("empty", Ctx.quadrantViable([]).ok, false);

// reasons are populated (honest, for the log)
const r = Ctx.quadrantViable(pts([[0, 0.44], [0, 0.44], [0, 0.44]]));
if (!r.reason) { console.error("FAIL: missing reason on withdrawal"); fail++; }

if (fail) { console.error(`QUADRANT GATE: ${fail} assertion(s) failed`); process.exit(1); }
console.log("QUADRANT GATE OK — both-axis dispersion + min-n gate rejects degenerate scatters");
