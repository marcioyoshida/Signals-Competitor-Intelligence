// #311: refuse an EAS build that would eat into the shared free-plan quota.
// Every app on the Expo account `marcioy` shares 15 iOS + 15 Android builds a month; a release
// must leave RESERVE builds per platform for the other apps. Reads `eas account:usage --json`
// on stdin (progress lines before the JSON are skipped). Exit 0 = go, 1 = refuse.
//   npx eas-cli account:usage marcioy --json | node scripts/quota-guard.mjs ios android
import { readFileSync } from "node:fs";

export const RESERVE = 2;

export function decide(usage, platforms, reserve = RESERVE) {
  const plan = String(usage?.account?.plan || "");
  if (!plan) return { ok: false, why: "no usage data" };           // never guess: refuse
  if (plan.toLowerCase() !== "free") return { ok: true, why: `${plan} plan` };
  const out = [];
  for (const p of platforms) {
    const q = usage?.builds?.[p]?.plan;
    if (!q || typeof q.used !== "number" || typeof q.limit !== "number")
      return { ok: false, why: `no usage data for ${p}` };
    const left = q.limit - q.used;
    if (left - 1 < reserve) return { ok: false, why: `${p}: ${q.used}/${q.limit} used, a build would leave ${left - 1} (< ${reserve} reserved)` };
    out.push(`${p} ${q.used}/${q.limit}`);
  }
  return { ok: true, why: out.join(", ") };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const raw = readFileSync(0, "utf8");
  const json = JSON.parse(raw.slice(raw.indexOf("\n{") + 1 || raw.indexOf("{")));
  const platforms = process.argv.slice(2).filter((p) => p === "ios" || p === "android");
  const d = decide(json, platforms.length ? platforms : ["ios", "android"]);
  console.log(`${d.ok ? "GO" : "REFUSE"}: ${d.why}`);
  process.exit(d.ok ? 0 : 1);
}
