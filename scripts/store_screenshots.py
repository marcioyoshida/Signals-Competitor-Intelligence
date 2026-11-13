"""#314: App Store / Google Play screenshots of the Onça app, rendered from the real /exec.

The page is the repo's `src/dashboard/site` served on 127.0.0.1, in APP MODE (the shell's
window.__ONCA_APP__), signed in with a placeholder token (the page only checks its expiry). The
feed is the **store reviewer tenant's real scoped feed** (`app-store-review`, #313), fetched
from the live feed Lambda with that tenant's JWT claims: public-source intelligence, no client
data, no decisions, no watchlists. Every other /api call answers empty. No password involved.

  .venv/bin/python -I scripts/store_screenshots.py [--out mobile/apps/onca/store/screenshots]

Sizes: iPhone 6.9" (1320x2868) and 6.5" (1284x2778), Android phone (1080x2160, Play max 2:1).
"""
from __future__ import annotations

import argparse
import base64
import gzip
import http.server
import json
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "src" / "dashboard" / "site"
FEED_FN = "OncaPrototypeStack-OncaFeedApi086A4381-BG0SDNWBQ32T"
TENANT = "app-store-review"
DEVICES = {                       # name: (css width, css height, scale)
    "iphone-6.9": (440, 956, 3),
    "iphone-6.5": (428, 926, 3),
    "android-phone": (360, 720, 3),     # Play: the long side may be at most 2x the short side
}
ROUTES = {"/exec": "v3/index.html", "/exec/": "v3/index.html"}
FEATURE_HTML = """<!doctype html><meta charset="utf-8"><style>
html,body{margin:0;width:1024px;height:500px;background:#0e1214;color:#e8edf0;font-family:system-ui,sans-serif}
.w{position:absolute;left:72px;top:150px}.m{font-size:96px;font-weight:800;letter-spacing:10px}.m b{color:#3ec39a}
.s{font-size:30px;font-weight:600;letter-spacing:6px;color:#9aa5ad;margin-top:6px}
.t{font-size:30px;margin-top:34px;color:#e8edf0}.r{position:absolute;right:-90px;top:-90px;width:420px;height:420px;
border-radius:50%;border:34px solid #1a2a26;box-shadow:0 0 0 34px #121c1a}</style>
<div class="r"></div><div class="w"><div class="m">ON<b>Ç</b>A</div><div class="s">SETORIAL</div>
<div class="t">Concorrentes e regulador do seu setor, no celular.</div></div>"""


def reviewer_feed() -> dict:
    import boto3
    ev = {"version": "2.0", "rawPath": "/api/feed", "headers": {"accept-encoding": "gzip"},
          "requestContext": {"http": {"method": "GET"},
                             "authorizer": {"jwt": {"claims": {"sub": "store-screens", "custom:tenant": TENANT}}}}}
    r = boto3.client("lambda", region_name="us-east-1").invoke(FunctionName=FEED_FN, Payload=json.dumps(ev).encode())
    out = json.loads(r["Payload"].read())
    if out.get("statusCode") != 200:
        raise SystemExit(f"feed for {TENANT}: HTTP {out.get('statusCode')}")
    body = out["body"]
    feed = json.loads(gzip.decompress(base64.b64decode(body)) if out.get("isBase64Encoded") else body)
    assert feed.get("tenant") == TENANT and not feed.get("tenant_usage") and not feed.get("mobile_usage")
    return feed


def _token() -> str:
    seg = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")  # noqa: E731
    return ".".join([seg({"alg": "none"}), seg({"sub": "store-screens", "email": "revisor@onssa.org", "custom:tenant": TENANT,
                                                  "custom:tier": "saas", "exp": int(time.time()) + 7200}), "x"])


class _H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(SITE), **k)

    def translate_path(self, path):
        p = path.split("?")[0].split("#")[0]
        return super().translate_path("/" + ROUTES[p] if p in ROUTES else p)

    def log_message(self, *a):
        pass


def main(argv: list[str] | None = None) -> int:
    from playwright.sync_api import sync_playwright
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "mobile" / "apps" / "onca" / "store" / "screenshots"))
    a = ap.parse_args(argv)
    out = Path(a.out)
    feed = json.dumps(reviewer_feed(), ensure_ascii=False)
    init = (f"window.__ONCA_APP__ = {{redirectUri: 'onca://auth', platform: 'ios', bridge: 4, version: '1.0.0', build: 1}};"
            "window.ReactNativeWebView = {postMessage: function () {}};"
            f"try {{ sessionStorage.setItem('onca_id_token', {json.dumps(_token())});"
            " sessionStorage.setItem('onca_app_started', '1'); } catch (e) {}")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    made: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for name, (w, h, scale) in DEVICES.items():
            d = out / name
            d.mkdir(parents=True, exist_ok=True)
            for theme in ("dark",):
                ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=scale,
                                          is_mobile=True, has_touch=True, color_scheme=theme, locale="pt-BR")
                ctx.add_init_script(init)
                page = ctx.new_page()

                def api(route):
                    url = route.request.url
                    if "/api/feed" in url:
                        return route.fulfill(status=200, content_type="application/json", body=feed)
                    return route.fulfill(status=200, content_type="application/json", body="{}")
                page.route("**/api/**", api)
                shots = [("01-hoje", base + "/exec#hoje", None),
                         ("02-painel", base + "/exec#painel", None),
                         ("03-cartao", base + "/exec#painel", "card"),
                         ("04-oficial", base + "/exec#painel", "officer")]
                for label, url, action in shots:
                    page.goto("about:blank")
                    page.goto(url)
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(900)
                    if action == "card":
                        rows = page.locator("#board [data-id], #board .row, #board article, #recs [data-id]")
                        if rows.count():
                            rows.first.click()
                            page.wait_for_timeout(700)
                    if action == "officer":
                        # ADR 032: tap Regulatório by name (the tab order changed; Risco names
                        # institutions in liquidation, a poor store first impression)
                        tab = page.locator('#officerTabs a[data-officer="cro"]')
                        if tab.count():
                            tab.first.click()
                            page.wait_for_timeout(700)
                    f = d / f"{label}.png"
                    page.screenshot(path=str(f))
                    made.append(str(f))
                ctx.close()
        # Google Play feature graphic (1024x500) and hi-res icon (512x512, from the app icon)
        ctx = browser.new_context(viewport={"width": 1024, "height": 500})
        page = ctx.new_page()
        page.set_content(FEATURE_HTML)
        page.wait_for_timeout(200)
        page.screenshot(path=str(out.parent / "feature-graphic.png"))
        icon = ROOT / "mobile" / "apps" / "onca" / "assets" / "icon.png"
        page.set_viewport_size({"width": 512, "height": 512})
        page.set_content("<style>html,body{margin:0;background:#0e1214}</style>"
                         f"<img src='data:image/png;base64,{base64.b64encode(icon.read_bytes()).decode()}' width=512 height=512>")
        page.wait_for_timeout(200)
        page.screenshot(path=str(out.parent / "icon-512.png"))
        made += [str(out.parent / "feature-graphic.png"), str(out.parent / "icon-512.png")]
        ctx.close()
        browser.close()
    srv.shutdown()
    print("\n".join(made))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
