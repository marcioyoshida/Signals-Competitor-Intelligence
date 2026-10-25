// ADR 029 (#254): the pure half of the WebView <-> shell bridge. No React Native imports, so
// `node --test` runs it directly (type stripping; no enums, explicit .ts imports).

export const ORIGIN = "https://onssa.org";
export const REDIRECT = "onca://auth";
export const COGNITO_ORIGIN = "https://auth.onssa.org";
/** The prefix domain still serves the same pool; kept so an older /exec keeps working. */
export const COGNITO_LEGACY_ORIGIN = "https://onca-668449743071.auth.us-east-1.amazoncognito.com";
/** Origins the WebView may render. Everything else opens in the system browser. */
const IN_APP_HOSTS = new Set(["onssa.org", "www.onssa.org"]);

/** Injected before /exec loads: tells context.js it runs inside the app (and which redirect). */
export function injectedFlag(platform: string): string {
  return `window.__ONCA_APP__ = ${JSON.stringify({ redirectUri: REDIRECT, platform, bridge: 3 })}; true;`;
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

/** How a sign-in attempt ended, as the shell saw it. */
export type AuthOutcome = "code" | "error" | "none" | "cancel" | "dismiss" | "locked" | "thrown";

/** A log-only GET that records how a sign-in attempt ended (CloudFront access log): the outcome
 *  and a short sanitized reason — never the code, a token or an email. */
export function authBeaconUrl(outcome: AuthOutcome, detail = ""): string {
  const d = detail.replace(/\S*@\S*/g, "*").replace(/[^A-Za-z0-9 _.:*-]/g, "").slice(0, 80);
  return `${ORIGIN}/v3/manifest.webmanifest?app_auth=${outcome}` + (d ? `&d=${encodeURIComponent(d)}` : "");
}

/** What the person sees when sign-in did not come back with a code (null = say nothing). */
export function authFailureMessage(outcome: AuthOutcome, detail = ""): string | null {
  if (outcome === "code" || outcome === "cancel") return null;
  if (outcome === "error") return `O provedor de login recusou a entrada${detail ? `: ${detail}` : ""}.`;
  if (outcome === "thrown") return `Não foi possível abrir o login${detail ? ` (${detail})` : ""}.`;
  return "O login foi fechado antes de voltar ao app. Tente de novo.";
}

/** The page asks the shell to open the Hosted UI; only our own Cognito domain is honoured. */
export function isHostedUiUrl(url: unknown): url is string {
  return typeof url === "string" && (url.startsWith(COGNITO_ORIGIN + "/") || url.startsWith(COGNITO_LEGACY_ORIGIN + "/"));
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

/** #258 (App Store 3.1.1): the app never opens a purchase surface — not even in the system browser. */
export function isPurchaseUrl(url: string): boolean {
  const h = hostOf(url);
  if (h === "signals-llc.store" || h === "www.signals-llc.store") return true;
  if (h !== null && IN_APP_HOSTS.has(h) && /^https:\/\/[^/]+\/pricing(\.html)?([?#]|$)/i.test(url)) return true;
  return /^mailto:/i.test(url) && /subject=[^&]*assinar/i.test(decodeURIComponentSafe(url));
}
function decodeURIComponentSafe(s: string): string { try { return decodeURIComponent(s); } catch { return s; } }

/** A card link: only onssa.org/c/<16 hex> (#289) or the older onssa.org/exec?c=<16 hex> (#170). */
export function isShareUrl(url: unknown): url is string {
  return typeof url === "string" &&
    /^https:\/\/(www\.)?onssa\.org\/(c\/[0-9a-f]{16}|exec\?c=[0-9a-f]{16})$/.test(url);
}

/** #289: an incoming universal / App Link. Only a #170 card link is honoured (it then opens in the
 *  WebView); anything else, e.g. the web sign-in return /exec?code=…, is ignored by the shell. */
export function incomingCardUrl(url: unknown): string | null {
  if (typeof url !== "string") return null;
  const u = url.replace(/^https:\/\/www\.onssa\.org\//, "https://onssa.org/");
  if (!isShareUrl(u)) return null;
  const m = /\/c\/([0-9a-f]{16})$/.exec(u);          // load the page that resolves it, not the edge redirect
  return m ? `https://onssa.org/exec?c=${m[1]}` : u;
}

/** A message posted by context.js through window.ReactNativeWebView. Requests that expect an
 *  answer carry an integer `id`; the shell replies with window.__oncaShell.reply(id, value). */
export type PageMessage =
  | { type: "auth" | "logout" | "share"; url: string }
  | { type: "push:token"; id: number; request: boolean }
  | { type: "lock:get"; id: number }
  | { type: "lock:set"; id: number; enabled: boolean; minutes: number };

export const LOCK_MINUTES = [1, 5, 15] as const;

export function parseMessage(data: string): PageMessage | null {
  try {
    const m = JSON.parse(data);
    if ((m?.type === "auth" || m?.type === "logout") && isHostedUiUrl(m.url)) return { type: m.type, url: m.url };
    if (m?.type === "share" && isShareUrl(m.url)) return { type: "share", url: m.url };
    const id = Number.isInteger(m?.id) && m.id > 0 ? m.id as number : 0;
    if (!id) return null;
    if (m.type === "push:token") return { type: "push:token", id, request: m.request === true };
    if (m.type === "lock:get") return { type: "lock:get", id };
    if (m.type === "lock:set" && typeof m.enabled === "boolean")
      return { type: "lock:set", id, enabled: m.enabled,
               minutes: (LOCK_MINUTES as readonly number[]).includes(m.minutes) ? m.minutes : 5 };
  } catch { /* not ours */ }
  return null;
}

/** The JS that answers a page request (value is JSON-encoded; id is an integer we parsed). */
export function replyScript(id: number, value: unknown): string {
  return `window.__oncaShell && window.__oncaShell.reply(${Math.trunc(id)}, ${JSON.stringify(value ?? null)}); true;`;
}

/** #257: lock again after `minutes` in the background; a cold start with the lock on is locked. */
export function shouldLock(enabled: boolean, minutes: number, backgroundedAt: number | null, now: number): boolean {
  if (!enabled) return false;
  if (backgroundedAt === null) return true;
  return now - backgroundedAt >= minutes * 60_000;
}
