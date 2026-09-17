"""OncaQaPipeline skeleton Lambda task (#127).

The FIRST proof that the container image, IAM, and S3 artifact path all work end to end,
before any real test pillar (#128-#133) is added: opens `/exec` (basic-auth gated), asserts
the page loads with the expected title, uploads a screenshot + a JSON result to the QA
artifacts bucket. Deliberately trivial — this is infra plumbing, not a real navigation test.
"""
from __future__ import annotations

import json
import os
import time

import boto3
from playwright.sync_api import sync_playwright

from qa_pipeline.lib import config

ARTIFACTS_BUCKET = os.environ.get("ONCA_QA_ARTIFACTS_BUCKET", "")
EXPECTED_TITLE = "Onça · Sala Executiva"


def lambda_handler(event, context):
    event = event or {}
    url = f"{config.SITE_URL}/exec"
    user, pw = config.basic_auth()
    run_id = event.get("run_id") or time.strftime("%Y%m%dT%H%M%SZ")
    screenshot_path = "/tmp/skeleton.png"

    with sync_playwright() as p:
        # Lambda-specific launch flags, confirmed live (#127): without them Chromium's
        # own sandbox init fails outright (no seccomp/user-namespace privileges in the
        # Lambda execution environment) and it also crashes using /dev/shm (Lambda's
        # default is far too small for Chromium's shared-memory needs) — both are the
        # standard containerized-Chromium workarounds, not Onça-specific.
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                # Chromium's normal multi-process model (separate renderer/GPU
                # processes) needs process-fork/namespace privileges Lambda's
                # container doesn't grant — confirmed live: launch() succeeded but
                # new_page() crashed the renderer target every time until these were
                # added. --single-process folds everything into one process.
                "--single-process", "--no-zygote",
            ],
        )
        ctx = browser.new_context(http_credentials={"username": user, "password": pw})
        page = ctx.new_page()
        page.goto(url, wait_until="networkidle", timeout=30_000)
        title = page.title()
        page.screenshot(path=screenshot_path)
        browser.close()

    ok = title == EXPECTED_TITLE
    result = {"ok": ok, "title": title, "url": url, "run_id": run_id}

    if ARTIFACTS_BUCKET:
        s3 = boto3.client("s3")
        s3.upload_file(screenshot_path, ARTIFACTS_BUCKET, f"{run_id}/skeleton.png")
        s3.put_object(
            Bucket=ARTIFACTS_BUCKET, Key=f"{run_id}/skeleton-result.json",
            Body=json.dumps(result).encode(), ContentType="application/json",
        )

    if not ok:
        raise AssertionError(f"unexpected /exec title: {title!r}")
    return result
