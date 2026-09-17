"""OncaQaPipeline Lambda task.

Dispatches on `event["mode"]`:
  - "skeleton" (default) — #127's original proof that the container image, IAM, and S3
    artifact path all work end to end: opens `/exec`, asserts the title, uploads a
    screenshot + result JSON.
  - "login" — #128: Hosted UI login for a QA persona (`qa_pipeline.lib.auth`), returns the
    captured sessionStorage so a downstream Map shard can seed it without re-authenticating.
  - "matrix" — #128: opens `/exec` in the given browser engine at the given viewport,
    seeded with a previously captured sessionStorage, asserts no responsive-layout overflow
    (the #125 mobile-responsive contract's actual, checkable signature) and the expected
    title, uploads a screenshot + result JSON per {browser, viewport} shard.
  - "smoke" — #129: smoke & critical-path navigation (auth flows, core action, officer/
    theme/drawer interactions, /entry + /v2/admin reachability). See qa_pipeline/checks/smoke.py.
  - "routing" — #130: deep-linking, dynamic routing (?admin=1&opkey=...), browser history.
    See qa_pipeline/checks/routing.py.
  - "resilience" — #131: broken-link scan (hard gate) + network interception (offline/slow/
    aborted API calls). See qa_pipeline/checks/resilience.py.
"""
from __future__ import annotations

import json
import time

from playwright.sync_api import sync_playwright

from qa_pipeline.checks import resilience, routing, smoke
from qa_pipeline.lib import auth, config
from qa_pipeline.lib.browser import (
    ARTIFACTS_BUCKET,
    CHROMIUM_LAUNCH_ARGS,
    EXPECTED_TITLE,
    VIEWPORTS,
    engine,
    launch_args,
    upload_artifact,
)


def _run_login(event: dict) -> dict:
    persona = event.get("persona", "entry")
    creds = config.qa_credentials()["personas"][persona]
    user, pw = config.basic_auth()
    url = f"{config.SITE_URL}/exec"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROMIUM_LAUNCH_ARGS)
        ctx = browser.new_context(http_credentials={"username": user, "password": pw})
        page = ctx.new_page()
        storage = auth.login_with_password(page, url, creds["username"], creds["password"])
        browser.close()
    return {"persona": persona, "tenant_id": creds["tenant_id"], "session_storage": storage}


def _run_matrix(event: dict) -> dict:
    browser_name = event.get("browser", "chromium")
    viewport_key = event.get("viewport", "desktop")
    viewport = VIEWPORTS[viewport_key]
    session_storage = event.get("session_storage") or {}
    run_id = event.get("run_id") or time.strftime("%Y%m%dT%H%M%SZ")
    user, pw = config.basic_auth()
    url = f"{config.SITE_URL}/exec"
    screenshot_path = "/tmp/matrix.png"

    with sync_playwright() as p:
        eng = engine(p, browser_name)
        browser = eng.launch(headless=True, args=launch_args(browser_name))
        ctx = browser.new_context(
            http_credentials={"username": user, "password": pw}, viewport=viewport,
        )
        if session_storage:
            auth.seed_session_storage(ctx, session_storage)
        page = ctx.new_page()
        page.goto(url, wait_until="networkidle", timeout=30_000)
        page.wait_for_timeout(1000)  # let boot()'s render settle before measuring layout
        title = page.title()
        scroll_width = page.evaluate("() => document.documentElement.scrollWidth")
        client_width = page.evaluate("() => document.documentElement.clientWidth")
        page.screenshot(path=screenshot_path, full_page=True)
        browser.close()

    # A collapsed responsive layout must never force horizontal scroll — the #125
    # contract's actual, checkable signature (not just "the page loaded"). A small
    # tolerance absorbs scrollbar-width rounding across engines.
    no_overflow = scroll_width <= client_width + 4
    ok = title == EXPECTED_TITLE and no_overflow
    result = {
        "ok": ok, "browser": browser_name, "viewport": viewport_key, "title": title,
        "scroll_width": scroll_width, "client_width": client_width, "run_id": run_id,
    }
    key_prefix = f"{run_id}/matrix/{browser_name}-{viewport_key}"
    upload_artifact(ARTIFACTS_BUCKET, f"{key_prefix}.png", path=screenshot_path)
    upload_artifact(ARTIFACTS_BUCKET, f"{key_prefix}.json", body=json.dumps(result).encode())

    if not ok:
        raise AssertionError(
            f"{browser_name}/{viewport_key}: title={title!r} "
            f"scroll_width={scroll_width} client_width={client_width}"
        )
    return result


def _run_skeleton(event: dict) -> dict:
    url = f"{config.SITE_URL}/exec"
    user, pw = config.basic_auth()
    run_id = event.get("run_id") or time.strftime("%Y%m%dT%H%M%SZ")
    screenshot_path = "/tmp/skeleton.png"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROMIUM_LAUNCH_ARGS)
        ctx = browser.new_context(http_credentials={"username": user, "password": pw})
        page = ctx.new_page()
        page.goto(url, wait_until="networkidle", timeout=30_000)
        title = page.title()
        page.screenshot(path=screenshot_path)
        browser.close()

    ok = title == EXPECTED_TITLE
    result = {"ok": ok, "title": title, "url": url, "run_id": run_id}
    upload_artifact(ARTIFACTS_BUCKET, f"{run_id}/skeleton.png", path=screenshot_path)
    upload_artifact(ARTIFACTS_BUCKET, f"{run_id}/skeleton-result.json", body=json.dumps(result).encode())

    if not ok:
        raise AssertionError(f"unexpected /exec title: {title!r}")
    return result


_DISPATCH = {
    "login": _run_login,
    "matrix": _run_matrix,
    "smoke": smoke.run,
    "routing": routing.run,
    "resilience": resilience.run,
}


def lambda_handler(event, context):
    event = event or {}
    mode = event.get("mode", "skeleton")
    return _DISPATCH.get(mode, _run_skeleton)(event)
