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
"""
from __future__ import annotations

import json
import os
import time

import boto3
from playwright.sync_api import sync_playwright

from qa_pipeline.lib import auth, config

ARTIFACTS_BUCKET = os.environ.get("ONCA_QA_ARTIFACTS_BUCKET", "")
EXPECTED_TITLE = "Onça · Sala Executiva"

# Confirmed live (#127): without these, Chromium fails in two distinct ways inside the
# Lambda execution environment — sandbox init fails outright (no seccomp/user-namespace
# privileges: --no-sandbox --disable-dev-shm-usage fixes it), then the renderer target
# still crashes on new_page() because Chromium's normal multi-process model needs
# process-fork privileges Lambda doesn't grant either (--single-process --no-zygote fixes
# that). Chromium-specific flags — Firefox/WebKit take neither the same flags nor, so far,
# needed any workaround of their own (see qa_pipeline/README.md for what was tested).
CHROMIUM_LAUNCH_ARGS = [
    "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
    "--single-process", "--no-zygote",
]

# The two breakpoints /exec's mobile-responsive pass (#125) actually targets
# (`@media (max-width: 860px)` / `(max-width: 480px)`, src/dashboard/site/v3/index.html),
# not arbitrary device names, plus one desktop baseline.
VIEWPORTS = {
    "desktop": {"width": 1920, "height": 1080},
    "tablet": {"width": 860, "height": 1024},
    "phone": {"width": 480, "height": 900},
}

ENGINES = ("chromium", "firefox", "webkit")


def _engine(p, browser: str):
    if browser not in ENGINES:
        raise ValueError(f"unknown browser {browser!r}, expected one of {ENGINES}")
    return getattr(p, browser)


def _launch_args(browser: str) -> list[str]:
    return CHROMIUM_LAUNCH_ARGS if browser == "chromium" else []


def _upload_artifact(bucket: str, key: str, *, path: str | None = None, body: bytes | None = None) -> None:
    if not bucket:
        return
    s3 = boto3.client("s3")
    if path is not None:
        s3.upload_file(path, bucket, key)
    else:
        s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")


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
        engine = _engine(p, browser_name)
        browser = engine.launch(headless=True, args=_launch_args(browser_name))
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
    _upload_artifact(ARTIFACTS_BUCKET, f"{key_prefix}.png", path=screenshot_path)
    _upload_artifact(ARTIFACTS_BUCKET, f"{key_prefix}.json", body=json.dumps(result).encode())

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
    _upload_artifact(ARTIFACTS_BUCKET, f"{run_id}/skeleton.png", path=screenshot_path)
    _upload_artifact(ARTIFACTS_BUCKET, f"{run_id}/skeleton-result.json", body=json.dumps(result).encode())

    if not ok:
        raise AssertionError(f"unexpected /exec title: {title!r}")
    return result


def lambda_handler(event, context):
    event = event or {}
    mode = event.get("mode", "skeleton")
    if mode == "login":
        return _run_login(event)
    if mode == "matrix":
        return _run_matrix(event)
    return _run_skeleton(event)
