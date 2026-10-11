import { test } from "node:test";
import assert from "node:assert/strict";
import { parseAuthRedirect, exchangeUrl, staysInApp, parseMessage, injectedFlag, COGNITO_ORIGIN } from "./bridge.ts";

test("auth redirect: code accepted only in Cognito's alphabet", () => {
  assert.deepEqual(parseAuthRedirect("onca://auth?code=abc123-XYZ_9"), { kind: "code", code: "abc123-XYZ_9" });
  assert.deepEqual(parseAuthRedirect("onca://auth?code=%22%3E%3Cscript%3E"), { kind: "none" });
  assert.deepEqual(parseAuthRedirect("onca://auth?error=access_denied&error_description=user+cancelled"),
    { kind: "error", error: "user cancelled" });
  assert.deepEqual(parseAuthRedirect("https://onssa.org/exec?code=abcdefgh"), { kind: "none" });
});

test("the exchange happens on /exec in the same WebView", () => {
  assert.equal(exchangeUrl("abc123-XYZ_9"), "https://onssa.org/exec?code=abc123-XYZ_9");
});

test("only onssa.org renders in the app; the rest goes to the system browser", () => {
  for (const u of ["https://onssa.org/exec", "https://www.onssa.org/v2/app/", "about:blank"]) assert.ok(staysInApp(u), u);
  for (const u of ["https://www.bcb.gov.br/x", "https://signals-llc.store/?product=onca", `${COGNITO_ORIGIN}/login`,
                   "https://onssa.org.evil.example/", "http://onssa.org/exec", "javascript:alert(1)"]) {
    assert.ok(!staysInApp(u), u);
  }
});

test("page messages: only auth/logout to our own Hosted UI", () => {
  assert.deepEqual(parseMessage(JSON.stringify({ type: "auth", url: `${COGNITO_ORIGIN}/login?x=1` })),
    { type: "auth", url: `${COGNITO_ORIGIN}/login?x=1` });
  assert.equal(parseMessage(JSON.stringify({ type: "auth", url: "https://evil.example/login" })), null);
  assert.equal(parseMessage(JSON.stringify({ type: "open", url: `${COGNITO_ORIGIN}/login` })), null);
  assert.equal(parseMessage("not json"), null);
});

test("injected flag is what context.js checks for", () => {
  assert.match(injectedFlag("ios"), /window\.__ONCA_APP__ = \{"redirectUri":"onca:\/\/auth","platform":"ios","bridge":1\}/);
});

import { isPurchaseUrl, isShareUrl } from "./bridge.ts";

test("#258: purchase surfaces are never opened from the app", () => {
  for (const u of ["https://signals-llc.store/?product=onca&tier=entry", "https://www.signals-llc.store/",
                   "https://onssa.org/pricing.html", "https://onssa.org/pricing", "https://www.onssa.org/pricing.html#x",
                   "mailto:contato@onssa.org?subject=Assinar%20On%C3%A7a%20%E2%80%94%20Entry"]) {
    assert.ok(isPurchaseUrl(u), u);
  }
  for (const u of ["https://onssa.org/exec", "https://onssa.org/docs/privacy.html", "https://www.bcb.gov.br/x",
                   "mailto:contato@onssa.org?subject=Ajuda"]) {
    assert.ok(!isPurchaseUrl(u), u);
  }
});

test("#258: only /exec?c=<16 hex> card links are shared natively", () => {
  assert.ok(isShareUrl("https://onssa.org/exec?c=0123456789abcdef"));
  for (const u of ["https://onssa.org/exec?c=XYZ", "https://evil.example/exec?c=0123456789abcdef", "javascript:1", 7]) {
    assert.ok(!isShareUrl(u), String(u));
  }
});
