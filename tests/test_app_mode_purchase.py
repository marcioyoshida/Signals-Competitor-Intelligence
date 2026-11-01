"""#258 (App Store 3.1.1): no purchase URL is reachable from any app-mode view.

Inside the store app the shell injects ``window.__ONCA_APP__`` before every page loads, refuses
purchase navigations (bridge.ts ``isPurchaseUrl``), and the pages remove the affordance itself:
context.js renders no CTA in app mode, and ``/v2/app-guard.js`` turns any purchase link left on a
page (static or added later) into the plain plan note.

Three layers:
  * static (hermetic): crawl the pages the app can reach from /exec through same-origin links; each
    loads app-guard.js first thing in <head>, and none links to a purchase surface outside
    ``data-web-only`` copy;
  * render (headless Chromium, 127.0.0.1 only, ``network`` marker): every reachable page in app
    mode has no purchase link and no purchase wording, also after a script inserts one; the same
    page in a browser keeps its links (no regression on the web);
  * the context.js CTA renderers (trial bar, lapsed/expired gate, Entry board, segment gate) in app
    mode vs the web, with data that makes the web show every CTA.
"""
from __future__ import annotations

import http.server
import re
import threading
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs

import pytest

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "src" / "dashboard" / "site"
GUARD = '<script src="/v2/app-guard.js"></script>'

# Paths the app routes to a page file (CloudFront rewrites /exec → /v3/index.html).
ROUTES = {"/exec": "v3/index.html", "/exec/": "v3/index.html", "/": "index.html"}
# Never reachable in the app: the shell refuses them (isPurchaseUrl) — they ARE the purchase surface.
SHELL_REFUSED = {"/pricing.html", "/pricing"}


def is_purchase(href: str) -> bool:
    """Mirror of bridge.ts isPurchaseUrl / app-guard.js isPurchase, for relative or absolute hrefs."""
    href = (href or "").strip()
    if href.lower().startswith("mailto:"):
        return bool(re.search(r"subject=[^&]*assinar", unquote(href), re.I))
    u = urlparse(href)
    host = (u.hostname or "").lower()
    if host in ("signals-llc.store", "www.signals-llc.store"):
        return True
    if host and host not in ("onssa.org", "www.onssa.org", "127.0.0.1"):
        return False
    if re.fullmatch(r"/pricing(\.html)?", u.path, re.I):
        return True
    return bool(re.fullmatch(r"/exec/?", u.path, re.I)) and "assinar" in parse_qs(u.query)


def test_matcher_agrees_with_the_shell_corpus():
    yes = ["https://signals-llc.store/?product=onca", "/pricing.html", "/pricing", "https://onssa.org/pricing.html#saas",
           "/exec?assinar=banking", "https://onssa.org/exec/?x=1&assinar=combo:pagamentos",
           "mailto:contato@onssa.org?subject=Assinar%20On%C3%A7a%20%E2%80%94%20Entry"]
    no = ["/exec", "/exec?c=0123456789abcdef", "/docs/", "/sample/", "https://www.bcb.gov.br/x",
          "mailto:contato@onssa.org", "mailto:contato@onssa.org?subject=Excluir%20minha%20conta",
          "https://example.org/pricing.html", "/docs/pricing-notes.html"]
    assert all(is_purchase(u) for u in yes), [u for u in yes if not is_purchase(u)]
    assert not any(is_purchase(u) for u in no), [u for u in no if is_purchase(u)]


def _page_file(path: str) -> Path | None:
    path = path.split("#")[0].split("?")[0]
    if path in ROUTES:
        return SITE / ROUTES[path]
    f = SITE / path.lstrip("/")
    if path.endswith("/"):
        f = f / "index.html"
    return f if f.suffix == ".html" and f.is_file() else None


_WEB_ONLY = re.compile(r"<(\w+)[^>]*\bdata-web-only\b[^>]*>.*?</\1>", re.S)


def _links(text: str) -> list[str]:
    return re.findall(r'href="([^"$`{}]+)"', text)


def reachable_pages() -> dict[str, Path]:
    """Same-origin pages reachable from /exec through links in the shipped HTML and scripts.
    Hrefs inside data-web-only copy don't count (app-guard hides it); shell-refused ones stop."""
    start = {"/exec": SITE / "v3/index.html"}
    extra_src = [SITE / "v2" / "context.js"]      # /exec's renderers live here
    seen: dict[str, Path] = {}
    todo = list(start.items())
    while todo:
        path, f = todo.pop()
        if f in seen.values():
            continue
        seen[path] = f
        text = _WEB_ONLY.sub("", f.read_text(encoding="utf-8"))
        if f == SITE / "v3/index.html":
            text += "".join(s.read_text(encoding="utf-8") for s in extra_src)
        for href in _links(text):
            if href.startswith(("http:", "https:", "mailto:", "tel:", "#", "//", "data:", "javascript:")):
                continue
            p = href.split("#")[0].split("?")[0]
            if not p.startswith("/") or p in SHELL_REFUSED:
                continue
            nf = _page_file(p)
            if nf is not None and nf not in seen.values():
                todo.append((p, nf))
    return seen


def test_the_crawl_finds_the_app_reachable_pages():
    got = {str(f.relative_to(SITE)) for f in reachable_pages().values()}
    for must in ("v3/index.html", "docs/index.html", "docs/privacy.html", "docs/excluir-conta.html",
                 "docs/celular-seguranca.html", "sample/index.html"):
        assert must in got, (must, sorted(got))


def test_every_app_reachable_page_loads_the_guard_first():
    for path, f in reachable_pages().items():
        text = f.read_text(encoding="utf-8")
        head = text[text.index("<head"):]
        first_script = head.index("<script")
        assert head[first_script:].startswith(GUARD), f"{path} ({f.relative_to(SITE)}): app-guard.js must be the first script"


def test_no_static_purchase_link_outside_web_only_copy():
    """Belt and braces: the guard would rewrite these, but static markup shouldn't rely on it."""
    for path, f in reachable_pages().items():
        if f.name == "index.html" and f.parent == SITE / "v3":
            continue   # /exec renders its CTAs from JS; the render layer below covers it
        bad = [h for h in _links(_WEB_ONLY.sub("", f.read_text(encoding="utf-8"))) if is_purchase(h)]
        assert not bad, (path, bad)


# --------------------------------------------------------------------------------- render -------
APP_INIT = """
window.__ONCA_APP__ = {redirectUri: "onca://auth", platform: "android", bridge: 3};
window.ReactNativeWebView = {postMessage: function (m) { (window.__shellMsgs = window.__shellMsgs || []).push(m); }};
"""
PURCHASE_WORDS = re.compile(r"\bAssin(ar|e)\b|planos e preços|Ver planos|R\$\s*\d|Fazer upgrade", re.I)


class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(SITE), **k)

    def translate_path(self, path):
        p = path.split("?")[0].split("#")[0]
        return super().translate_path("/" + ROUTES[p] if p in ROUTES else p)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def site():
    sync_api = pytest.importorskip("playwright.sync_api")
    pw = sync_api.sync_playwright().start()
    try:
        browser = pw.chromium.launch()
    except Exception as exc:  # noqa: BLE001 - no browser installed here
        pw.stop()
        pytest.skip(f"Chromium not available: {str(exc)[:80]}")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"base": f"http://127.0.0.1:{srv.server_address[1]}", "browser": browser}
    srv.shutdown()
    browser.close()
    pw.stop()


def _open(site, path, *, app):
    ctx = site["browser"].new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    if app:
        ctx.add_init_script(APP_INIT)
    page = ctx.new_page()
    # no backend here: every API answers 503 (the pages' own "unavailable" branches render too)
    page.route("**/api/**", lambda r: r.fulfill(status=503, content_type="application/json", body="{}"))
    page.goto(site["base"] + path)
    page.wait_for_load_state("networkidle")
    return page


def _hrefs(page) -> list[str]:
    return page.eval_on_selector_all("a[href]", "els => els.map(a => a.getAttribute('href'))")


def _visible_text(page) -> str:
    return page.evaluate("document.body.innerText")


@pytest.mark.network
def test_no_purchase_link_or_wording_on_any_app_reachable_page(site):
    for path, f in reachable_pages().items():
        page = _open(site, path, app=True)
        try:
            bad = [h for h in _hrefs(page) if is_purchase(h)]
            assert not bad, (path, bad)
            m = PURCHASE_WORDS.search(_visible_text(page))
            assert not m, (path, m.group(0))
            # a link a script adds later is rewritten too
            page.evaluate("""document.body.insertAdjacentHTML('beforeend',
              '<p id=late><a href="https://signals-llc.store/?product=onca">Assinar</a> <a href="/pricing.html">x</a> <a href="/docs/">guia</a></p>')""")
            page.wait_for_timeout(50)
            late = page.eval_on_selector_all("#late a", "els => els.map(a => a.getAttribute('href'))")
            assert late == ["/docs/"], (path, late)
            assert page.inner_text("#late").count("Gerencie seu plano em onssa.org.") == 2
        finally:
            page.context.close()


@pytest.mark.network
def test_the_web_keeps_its_pricing_links(site):
    page = _open(site, "/docs/", app=False)
    try:
        assert "/pricing.html" in _hrefs(page)
        assert page.evaluate("typeof window.__oncaAppGuard") == "undefined"
        assert not page.evaluate("document.documentElement.classList.contains('onca-app')")
    finally:
        page.context.close()


# Data that makes the WEB render every CTA: a trial, a lapsed and an expired subscription, an Entry
# tenant with a SaaS-band module, and a segment outside the licence.
RENDER_CTAS = """async ({}) => {
  const C = window.OncaCtx, up = {ok: true, storefront: "https://signals-llc.store"};
  const soon = new Date(Date.now() + 5 * 86400000).toISOString();
  const out = {};
  out.trial = C.billingBannerHTML({billing: {state: "trial", trial_until: soon, trial_modules: ["fintech"]}, upgrade: up});
  const el = document.createElement("div"); document.body.appendChild(el);
  C.mountGate(el, "noAccess", "o painel", {billing: {state: "lapsed", lapsed_modules: ["banking"], lapsed_combos: []}, upgrade: up});
  out.lapsed = el.innerHTML;
  C.mountGate(el, "noAccess", "o painel", {billing: {state: "trial_expired", trial_modules: ["fintech"]}, upgrade: up});
  out.expired = el.innerHTML;
  out.segment = C.segmentGateHTML({tier: "saas", upgrade: up}, {slug: "insurance", licensed: ["banking"]});
  C.mountEntryBoard({tier: "entry", scoped_modules: ["fintech"], upgrade: up, cards: [], kpis: {}});
  out.entry = (document.getElementById("board") || {}).innerHTML || "";
  return out;
}"""


def _render_ctas(site, *, app):
    page = _open(site, "/exec", app=app)
    try:
        page.wait_for_function("() => !!(window.OncaCtx && window.OncaCtx.mountEntryBoard)")
        return page.evaluate(RENDER_CTAS, {})
    finally:
        page.context.close()


@pytest.mark.network
def test_context_js_renders_no_purchase_cta_in_app_mode(site):
    web = _render_ctas(site, app=False)
    app = _render_ctas(site, app=True)
    for view, html in web.items():          # the fixture data really does produce a CTA on the web
        assert any(is_purchase(h) for h in _links(html)), (view, "web shows no CTA: the fixture is stale")
    for view, html in app.items():
        bad = [h for h in _links(html) if is_purchase(h)]
        assert not bad, (view, bad)
        assert not PURCHASE_WORDS.search(re.sub(r"<[^>]+>", " ", html)), (view, PURCHASE_WORDS.search(html).group(0))
    for view in ("trial", "lapsed", "expired", "entry"):
        assert "Gerencie seu plano em onssa.org." in app[view], view
