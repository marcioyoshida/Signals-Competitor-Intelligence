"""#312/#318: /v2/app-guard.js in the store app — health beacon and the minimum-version prompt.

Rendered in headless Chromium against the real file (127.0.0.1 only, `network` marker)."""
from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path

import pytest

SITE = Path(__file__).resolve().parents[1] / "src" / "dashboard" / "site"
PAGE = b"""<!doctype html><html><head><script src="/v2/app-guard.js"></script></head>
<body><main>Painel</main><script>window.OncaCtx = {logout: function () { window.__loggedOut = true; }};</script></body></html>"""
APP = """window.__ONCA_APP__ = {redirectUri: "onca://auth", platform: "ios", bridge: 4, version: "0.2.0", build: 7};
window.ReactNativeWebView = {postMessage: function () {}};"""


class _H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(SITE), **k)

    def do_GET(self):
        if self.path.startswith("/page"):
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.end_headers()
            self.wfile.write(PAGE)
            return
        super().do_GET()

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def site():
    sync_api = pytest.importorskip("playwright.sync_api")
    pw = sync_api.sync_playwright().start()
    try:
        browser = pw.chromium.launch()
    except Exception as exc:  # noqa: BLE001
        pw.stop()
        pytest.skip(f"Chromium not available: {str(exc)[:80]}")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"base": f"http://127.0.0.1:{srv.server_address[1]}", "browser": browser}
    srv.shutdown()
    browser.close()
    pw.stop()


def _open(site, verdict, *, app=True, feed_status=200):
    ctx = site["browser"].new_context(viewport={"width": 390, "height": 844})
    if app:
        ctx.add_init_script(APP)
    page = ctx.new_page()
    sent: list[dict] = []

    def beacon(route):
        sent.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"ok": True, "update": verdict, "store": "https://apps.apple.com/app/id6821796703"}))
    page.route("**/api/session/app", beacon)
    page.route("**/api/feed**", lambda r: r.fulfill(status=feed_status, content_type="application/json", body="{}"))
    page.goto(site["base"] + "/page")
    page.wait_for_load_state("networkidle")
    return page, sent


@pytest.mark.network
def test_start_is_reported_once_with_version_and_build(site):
    page, sent = _open(site, "ok")
    assert sent and sent[0]["kind"] == "start" and sent[0]["version"] == "0.2.0" and sent[0]["build"] == 7
    page.reload()
    page.wait_for_load_state("networkidle")
    assert [s["kind"] for s in sent].count("start") == 1          # once per app session
    assert page.locator(".onca-update").count() == 0


@pytest.mark.network
def test_hard_minimum_blocks_with_update_and_sign_out(site):
    page, _ = _open(site, "hard")
    box = page.locator(".onca-update-hard")
    assert box.is_visible() and "não é mais suportada" in box.inner_text()
    assert page.locator(".onca-update-btn").get_attribute("href").startswith("https://apps.apple.com/")
    page.locator(".onca-update-sec").click()
    assert page.evaluate("window.__loggedOut") is True
    page.reload()                                                  # still blocked on the next page
    page.wait_for_load_state("networkidle")
    assert page.locator(".onca-update-hard").is_visible()


@pytest.mark.network
def test_soft_minimum_is_a_dismissible_banner(site):
    page, _ = _open(site, "soft")
    assert page.locator(".onca-update").is_visible() and page.locator(".onca-update-hard").count() == 0
    page.locator(".onca-update-sec").click()
    assert page.locator(".onca-update").count() == 0


@pytest.mark.network
def test_failed_api_call_reports_route_and_status_only(site):
    page, sent = _open(site, "ok", feed_status=502)
    page.evaluate("fetch('/api/feed?token=secret#x').catch(function(){})")
    page.wait_for_timeout(500)
    errs = [s for s in sent if s["kind"] == "error"]
    assert errs and errs[0]["route"] == "/api/feed" and errs[0]["status"] == 502
    assert "secret" not in json.dumps(errs)


@pytest.mark.network
def test_inert_in_a_browser(site):
    page, sent = _open(site, "hard", app=False)
    assert sent == [] and page.locator(".onca-update").count() == 0
