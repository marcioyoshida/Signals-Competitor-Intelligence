"""Shared constants and helpers of the app-mode E2E (see conftest.py for what and how)."""
from __future__ import annotations

import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENABLED = os.environ.get("ONCA_E2E") == "1"
BASE_URL = os.environ.get("ONCA_E2E_BASE_URL", "https://onssa.org").rstrip("/")
AUTH_ORIGIN = os.environ.get("ONCA_E2E_AUTH_ORIGIN", "https://auth.onssa.org").rstrip("/")
OUT = Path(os.environ.get("ONCA_E2E_OUT", "/tmp/onca-e2e"))
QA_SECRET = "signalscompetitor/onca/qa-test-credentials"
VIEWPORT = {"width": 390, "height": 844}

# What the native shell injects (bridge.ts injectedFlag + react-native-webview), with a recorder
# standing in for App.tsx onMessage. Runs before every document in the context. Requests that
# expect an answer (an integer id) get the answer App.tsx gives on a device with no push token
# and no screen lock (bridge.ts replyScript -> window.__oncaShell.reply) — the page waits for
# them (e.g. logout first asks for the push token to unsubscribe it).
APP_INIT = """
window.__ONCA_APP__ = {redirectUri: "onca://auth", platform: "ios", bridge: 2};
window.__shellMsgs = [];
window.ReactNativeWebView = { postMessage: function (m) {
  window.__shellMsgs.push(String(m));
  var d = null; try { d = JSON.parse(m); } catch (e) {}
  if (!d || !(d.id > 0)) return;
  var v = d.type === "push:token" ? {reason: "alertas indisponíveis neste aparelho"}
        : d.type === "lock:get" ? {enabled: false, minutes: 5, available: false}
        : d.type === "lock:set" ? {ok: false, reason: "defina um bloqueio de tela no aparelho primeiro"} : null;
  setTimeout(function () { window.__oncaShell && window.__oncaShell.reply(d.id, v); }, 0);
} };
"""


def device(pw):
    d = dict(pw.devices["iPhone 15"])
    d.pop("default_browser_type", None)
    d.update(viewport=dict(VIEWPORT), screen=dict(VIEWPORT), is_mobile=True, has_touch=True)
    d.update(locale="pt-BR", timezone_id="America/Sao_Paulo")
    return d


class Watch:
    """Console errors + uncaught page errors of one page."""

    def __init__(self, page):
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        page.on("console", lambda m: self.console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: self.page_errors.append(str(e)))


def new_context(pw, browser, app_mode=True, **extra):
    ctx = browser.new_context(**device(pw), **extra)
    ctx.set_default_timeout(30_000)
    if app_mode:
        ctx.add_init_script(APP_INIT)
    return ctx


def shell_messages(page):
    return [json.loads(m) for m in page.evaluate("window.__shellMsgs || []")]


def shot(page, name, full_page=False):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.png"
    page.screenshot(path=str(path), full_page=full_page)
    return path


def attach(request, page):
    """Remember the page a test drives so a failure gets a screenshot."""
    request.node._e2e_page = page
    return page
