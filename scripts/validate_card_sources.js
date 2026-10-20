// #290: the drawer's card lookup and source list, run against v2/app.js as the browser loads it.
// Usage: node scripts/validate_card_sources.js [feed.json]  (no argument: built-in fixtures)
const fs = require("fs"), path = require("path"), vm = require("vm");
const win = {};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../src/dashboard/site/v2/app.js"), "utf8"),
  { window: win, document: { documentElement: { dataset: {} }, addEventListener() {} },
    localStorage: { getItem() { return null; }, setItem() {} }, matchMedia: () => ({ matches: false }),
    navigator: {}, console });
const U = win.OncaUI, fail = (m) => { console.error("FAIL " + m); process.exitCode = 1; };

const feed = process.argv[2] ? JSON.parse(fs.readFileSync(process.argv[2], "utf8")).feed : [
  { id: "a", date: "2026-10-19", run_at: "2026-10-19T12:42", citations: [{ url: "https://x.example/1" }] },
  { id: "a", date: "2026-10-06", run_at: "2026-10-06T12:45", citations: [{ id: "pix:1" }] },
  { id: "b", date: "2026-10-19", citations: [{ id: "juros-d:2026-09-28:1" }, { id: "bcb-auth:1" }, { id: "pix:2" }, { id: "pix:3" }] },
  { id: "c", date: "2026-10-19", citations: [{ id: "market:1", source: "BCB-IFDATA" }] },
];
const byId = U.newestById(feed);
let ids = 0, noLink = [];
for (const id of Object.keys(byId)) {
  ids++;
  const newest = feed.filter((c) => c.id === id).reduce((a, c) =>
    ((c.date || "") + "|" + (c.run_at || "")) > ((a.date || "") + "|" + (a.run_at || "")) ? c : a);
  if (byId[id] !== newest) fail(`${id}: lookup is not the newest copy`);
  const m = U.citationModel(byId[id]);
  if (!m.entries.length && byId[id].mode !== "derived") noLink.push(id);   // derived = silence etc.
  if (m.entries.some((e) => !/^https?:\/\//.test(e.url) || !e.label)) fail(`${id}: an entry without url/label`);
}
if (!process.argv[2]) {
  if (byId.a.date !== "2026-10-19") fail("a: oldest copy kept");
  const b = U.citationModel(byId.b).entries;
  if (b.length !== 3) fail(`b: expected 3 dataset lines (pix ids share one), got ${b.length}`);
  if (!b.some((e) => /Estatísticas do Pix/.test(e.label))) fail("b: no Pix dataset line");
  if (!/ifdata/.test(U.citationModel(byId.c).entries[0].url)) fail("c: IF.data not linked");
}
console.log(`ids ${ids}, non-derived cards whose newest copy has no source line: ${noLink.length}` +
  (noLink.length ? ` (${noLink.slice(0, 8).join(", ")})` : ""));
if (process.argv[2] && noLink.length) process.exitCode = 1;
