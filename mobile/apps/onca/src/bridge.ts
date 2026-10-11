// ADR 029 (#254): the pure half of the WebView <-> shell bridge. No React Native imports, so
// `node --test` runs it directly (type stripping; no enums, explicit .ts imports).

export const ORIGIN = "https://onssa.org";
export const REDIRECT = "onca://auth";
export const COGNITO_ORIGIN = "https://onca-668449743071.auth.us-east-1.amazoncognito.com";
/** Origins the WebView may render. Everything else opens in the system browser. */
const IN_APP_HOSTS = new Set(["onssa.org", "www.onssa.org"]);

/** Injected before /exec loads: tells context.js it runs inside the app (and which redirect). */
export function injectedFlag(platform: string): string {
  return `window.__ONCA_APP__ = ${JSON.stringify({ redirectUri: REDIRECT, platform, bridge: 1 })}; true;`;
}

function query(url: string): Record<string, string> {
  const i = url.indexOf("?");
  const out: Record<string, string> = {};
  if (i < 0) return out;
  for (const part of url.slice(i + 1).split("#")[0].split("&")) {
    if (!part) continue;
    const [k, v = ""] = part.split("=");
    try { out[decodeURIComponent(k)] = decodeURIComponent(v.replace(/\+/g, " ")); } catch { /* skip */ }
  }
  return out;
}

export type AuthResult = { kind: "code"; code: string } | { kind: "error"; error: string } | { kind: "none" };

/** What came back on onca://auth. A code is accepted only in Cognito's alphabet. */
export function parseAuthRedirect(url: string): AuthResult {
  if (!url.startsWith(REDIRECT)) return { kind: "none" };
  const q = query(url);
  if (q.error) return { kind: "error", error: q.error_description || q.error };
  if (q.code && /^[A-Za-z0-9_-]{8,256}$/.test(q.code)) return { kind: "code", code: q.code };
  return { kind: "none" };
}

/** Where to send the WebView once a code arrives: /exec does the exchange itself. */
export function exchangeUrl(code: string): string {
  return `${ORIGIN}/exec?code=${encodeURIComponent(code)}`;
}

/** The page asks the shell to open the Hosted UI; only our own Cognito domain is honoured. */
export function isHostedUiUrl(url: unknown): url is string {
  return typeof url === "string" && url.startsWith(COGNITO_ORIGIN + "/");
}

function hostOf(url: string): string | null {
  const m = /^https:\/\/([^/?#:]+)/i.exec(url);
  return m ? m[1].toLowerCase() : null;
}

/** true = render in the WebView; false = hand to the system browser (or ignore). */
export function staysInApp(url: string): boolean {
  if (url === "about:blank" || url.startsWith("data:") || url.startsWith("blob:")) return true;
  const h = hostOf(url);
  return h !== null && IN_APP_HOSTS.has(h);
}

/** A message posted by context.js through window.ReactNativeWebView. */
export type PageMessage = { type: "auth" | "logout"; url: string };
export function parseMessage(data: string): PageMessage | null {
  try {
    const m = JSON.parse(data);
    if ((m?.type === "auth" || m?.type === "logout") && isHostedUiUrl(m.url)) return { type: m.type, url: m.url };
  } catch { /* not ours */ }
  return null;
}
