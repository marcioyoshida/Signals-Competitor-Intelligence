import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import vm from "node:vm";
import { STRANDED_SIGNOUT_JS, effectiveMinutes, isPurchaseUrl, parseMessage } from "./bridge.ts";

const require = createRequire(import.meta.url);
const { addRecentsPrivacy } = require("../plugins/recents-privacy.js");

test("#257: the tenant ceiling only shortens the delay", () => {
  assert.equal(effectiveMinutes(15, null), 15);
  assert.equal(effectiveMinutes(15, 5), 5);
  assert.equal(effectiveMinutes(1, 15), 1);          // a person may lock sooner than the tenant asks
  assert.equal(effectiveMinutes(7, null), 5);        // off-scale preference → the default
  assert.equal(effectiveMinutes(15, 0), 15);         // no usable ceiling
});

test("#257: lock:policy carries only an offered ceiling; lock:signedout has no payload", () => {
  assert.deepEqual(parseMessage(JSON.stringify({ type: "lock:policy", max: 5 })), { type: "lock:policy", max: 5 });
  assert.deepEqual(parseMessage(JSON.stringify({ type: "lock:policy", max: 60 })), { type: "lock:policy", max: null });
  assert.deepEqual(parseMessage(JSON.stringify({ type: "lock:policy" })), { type: "lock:policy", max: null });
  assert.deepEqual(parseMessage(JSON.stringify({ type: "lock:signedout", x: 1 })), { type: "lock:signedout" });
});

/** Runs the stranded sign-out script against a fake page and reports what it did. */
async function runStranded(page: { loggedIn: boolean; logoutThrows?: boolean }) {
  const did = { logout: 0, fetch: [] as string[], cleared: 0, posted: [] as string[] };
  const store = { clear: () => { did.cleared++; } };
  const ctx = {
    window: {
      ReactNativeWebView: { postMessage: (m: string) => did.posted.push(JSON.parse(m).type) },
      OncaCtx: {
        isLoggedIn: () => page.loggedIn,
        logout: async () => { did.logout++; if (page.logoutThrows) throw new Error("boom"); },
      },
    },
    fetch: async (u: string) => { did.fetch.push(u); return {}; },
    sessionStorage: store, localStorage: store, Promise,
  };
  vm.runInNewContext(STRANDED_SIGNOUT_JS, ctx);
  await new Promise((r) => setTimeout(r, 20));
  return did;
}

test("#257 stranded lock: signed in → /exec's own logout (which posts 'logout' itself)", async () => {
  const d = await runStranded({ loggedIn: true });
  assert.equal(d.logout, 1);
  assert.deepEqual(d.fetch, []);
  assert.deepEqual(d.posted, []);
});

test("#257 stranded lock: logout failing still revokes, clears and reports", async () => {
  const d = await runStranded({ loggedIn: true, logoutThrows: true });
  assert.deepEqual(d.fetch, ["/api/session/logout"]);
  assert.equal(d.cleared, 2);
  assert.deepEqual(d.posted, ["lock:signedout"]);
});

test("#257 stranded lock: signed out (or no /exec) → revoke + clear + report", async () => {
  const d = await runStranded({ loggedIn: false });
  assert.equal(d.logout, 0);
  assert.deepEqual(d.fetch, ["/api/session/logout"]);
  assert.deepEqual(d.posted, ["lock:signedout"]);
});

test("#258: the shell also refuses /exec?assinar=, and nothing else on /exec", () => {
  for (const u of ["https://onssa.org/exec?assinar=banking", "https://onssa.org/exec/?x=1&assinar=combo:pagamentos",
                   "https://www.onssa.org/exec?assinar=fintech"]) assert.ok(isPurchaseUrl(u), u);
  for (const u of ["https://onssa.org/exec", "https://onssa.org/exec?c=0123456789abcdef", "https://onssa.org/exec?code=abc",
                   "https://onssa.org/exec#assinar=x", "https://example.org/exec?assinar=banking"]) assert.ok(!isPurchaseUrl(u), u);
});

const KOTLIN = `class MainActivity : ReactActivity() {
  override fun onCreate(savedInstanceState: Bundle?) {
    setTheme(R.style.AppTheme);
    super.onCreate(null)
  }
}`;

test("#257 Android plugin: recents screenshot off on API 33+, added once", () => {
  const out = addRecentsPrivacy(KOTLIN, "kt");
  assert.match(out, /super\.onCreate\(null\)\n\s+\/\/ #257 recents privacy[^\n]*\n\s+if \(android\.os\.Build\.VERSION\.SDK_INT >= 33\) setRecentsScreenshotEnabled\(false\)\n/);
  assert.equal(addRecentsPrivacy(out, "kt"), out);
  const java = addRecentsPrivacy("void onCreate(Bundle b) {\n    super.onCreate(null);\n}", "java");
  assert.match(java, /setRecentsScreenshotEnabled\(false\);/);
  assert.throws(() => addRecentsPrivacy("class X {}", "kt"), /no super\.onCreate/);
});
