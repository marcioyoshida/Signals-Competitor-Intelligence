import { test } from "node:test";
import assert from "node:assert/strict";
// @ts-ignore - plain ESM helper
import { decide } from "../scripts/quota-guard.mjs";

const usage = (ios: number, android: number, plan = "Free") => ({
  account: { plan }, builds: { ios: { plan: { used: ios, limit: 15 } }, android: { plan: { used: android, limit: 15 } } } });

test("a build goes when it leaves the reserve for the other apps", () => {
  assert.equal(decide(usage(1, 1), ["ios", "android"]).ok, true);
  assert.equal(decide(usage(12, 0), ["ios"]).ok, true);        // leaves 2
});

test("a build is refused when it would eat the shared reserve", () => {
  const d = decide(usage(13, 0), ["ios"]);
  assert.equal(d.ok, false); assert.match(d.why, /ios: 13\/15/);
  assert.equal(decide(usage(0, 14), ["android"]).ok, false);
  assert.equal(decide({}, ["ios"]).ok, false);                 // no data: never guess
  assert.equal(decide(usage(15, 15, "Production"), ["ios"]).ok, true);
});
