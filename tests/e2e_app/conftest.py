"""Layer 1 of the Onça mobile-app E2E (#259): /exec in *app mode* in a real browser engine.

The store app (mobile/apps/onca) is a WebView of /exec. Before any page script runs, the shell
injects ``window.__ONCA_APP__`` (bridge.ts ``injectedFlag``) and react-native-webview provides
``window.ReactNativeWebView.postMessage``. These fixtures inject exactly that — plus a recorder in
place of the native side — so the page behaves as it does inside the app, and the tests can
assert what it asks the shell to do.

Opt-in only: the whole directory is skipped unless ``ONCA_E2E=1`` (it signs in to PRODUCTION
with the QA persona). Run it with ``scripts/e2e_app.sh``.

Environment:
  ONCA_E2E=1                 required, or every test here is skipped
  ONCA_E2E_BASE_URL          default https://onssa.org
  ONCA_E2E_AUTH_ORIGIN       expected Hosted UI origin, default https://auth.onssa.org
  ONCA_E2E_BROWSER           webkit | chromium | auto (default: WebKit if it launches, else Chromium)
  ONCA_E2E_PERSONA           QA persona in signalscompetitor/onca/qa-test-credentials (default admin:
                             the only one with the officer tab bar; Entry gets the Entry board)
  ONCA_E2E_OUT               screenshots, default /tmp/onca-e2e
  AWS_PROFILE                credentials for Secrets Manager (my2027); the secret is read in-process
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path

import pytest

from tests.e2e_app.harness import (
    BASE_URL, ENABLED, HERE, OUT, QA_SECRET, Watch, attach, new_context,
)


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason="app-mode E2E hits production: set ONCA_E2E=1 (scripts/e2e_app.sh)")
    for item in items:
        if HERE not in Path(str(item.fspath)).resolve().parents:
            continue
        item.add_marker(pytest.mark.e2e_app)
        item.add_marker(pytest.mark.network)   # root conftest socket guard: live I/O is declared
        if not ENABLED:
            item.add_marker(skip)


# --- report hook: the failing test's page is screenshotted -------------------------------------
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.when != "call" or not rep.failed:
        return
    page = getattr(item, "_e2e_page", None)
    if page is None:
        return
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        path = OUT / f"FAIL-{item.name}.png"
        page.screenshot(path=str(path), full_page=False)
        rep.sections.append(("e2e screenshot", str(path)))
    except Exception:  # the page may be gone; the assertion is the report
        pass


# --- live configuration ------------------------------------------------------------------------
def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"user-agent": "onca-e2e-app/1", "cache-control": "no-cache"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8")


@pytest.fixture(scope="session")
def live_auth():
    """AUTH.clientId / AUTH.domain as the LIVE site ships them (the pool cutover changes the id)."""
    js = _get(f"{BASE_URL}/v2/context.js?e2e={int(time.time())}")
    block = js[js.index("const AUTH = {"):]
    block = block[: block.index("};")]
    client = re.search(r'clientId:\s*"([^"]+)"', block)
    domain = re.search(r'domain:\s*"([^"]+)"', block)
    assert client and domain, "could not read AUTH.clientId / AUTH.domain from the live context.js"
    return {"client_id": client.group(1), "domain": domain.group(1).rstrip("/")}


@pytest.fixture(scope="session")
def qa_persona():
    """The QA persona's credentials, read in-process from Secrets Manager (never printed)."""
    import boto3

    name = os.environ.get("ONCA_E2E_PERSONA", "admin")
    sess = boto3.Session(profile_name=os.environ.get("AWS_PROFILE") or "my2027")
    raw = sess.client("secretsmanager", region_name="us-east-1").get_secret_value(SecretId=QA_SECRET)["SecretString"]
    personas = json.loads(raw)["personas"]
    assert name in personas, f"persona {name!r} not in {QA_SECRET} (have: {sorted(personas)})"
    p = personas[name]
    return {"name": name, "username": p["username"], "password": p["password"], "tier": p.get("tier", "")}


# --- browser -----------------------------------------------------------------------------------
@pytest.fixture(scope="session")
def pw():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        yield p


def _launch(pw, name):
    return getattr(pw, name).launch()


@pytest.fixture(scope="session")
def engine(pw):
    """(name, browser). WebKit is the closest to WKWebView; Chromium is the fallback."""
    want = os.environ.get("ONCA_E2E_BROWSER", "auto").lower()
    order = ["webkit", "chromium"] if want == "auto" else [want]
    errors = []
    for name in order:
        try:
            b = _launch(pw, name)
        except Exception as e:  # missing host libs (see scripts/e2e_app.sh --setup-webkit)
            errors.append(f"{name}: {str(e).splitlines()[0]}")
            continue
        print(f"\n[e2e_app] engine={name} {b.version} base={BASE_URL}")
        yield name, b
        b.close()
        return
    pytest.fail("no browser engine launched: " + " | ".join(errors))


@pytest.fixture()
def app_page(request, pw, engine):
    """A fresh, signed-out context in app mode (no cookies, no storage)."""
    ctx = new_context(pw, engine[1])
    page = ctx.new_page()
    page.watch = Watch(page)
    attach(request, page)
    yield page
    ctx.close()
