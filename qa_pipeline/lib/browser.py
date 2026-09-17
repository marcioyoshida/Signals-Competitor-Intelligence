"""Shared Playwright/Lambda browser-launch helpers, factored out of handler.py so both the
dispatcher and the per-pillar check modules (qa_pipeline/checks/*) can import them without
a circular dependency.
"""
from __future__ import annotations

import os

import boto3

ARTIFACTS_BUCKET = os.environ.get("ONCA_QA_ARTIFACTS_BUCKET", "")
EXPECTED_TITLE = "Onça · Sala Executiva"

# Confirmed live (#127): without these, Chromium fails in two distinct ways inside the
# Lambda execution environment — sandbox init fails outright (no seccomp/user-namespace
# privileges: --no-sandbox --disable-dev-shm-usage fixes it), then the renderer target
# still crashes on new_page() because Chromium's normal multi-process model needs
# process-fork privileges Lambda doesn't grant either (--single-process --no-zygote fixes
# that). Chromium-specific flags — Firefox takes neither the same flags nor, so far, needed
# any workaround of its own beyond the HOME/XDG env vars (see qa_pipeline/README.md).
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


def engine(p, browser: str):
    if browser not in ENGINES:
        raise ValueError(f"unknown browser {browser!r}, expected one of {ENGINES}")
    return getattr(p, browser)


def launch_args(browser: str) -> list[str]:
    return CHROMIUM_LAUNCH_ARGS if browser == "chromium" else []


def upload_artifact(bucket: str, key: str, *, path: str | None = None, body: bytes | None = None) -> None:
    if not bucket:
        return
    s3 = boto3.client("s3")
    if path is not None:
        s3.upload_file(path, bucket, key)
    else:
        s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
