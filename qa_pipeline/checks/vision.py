"""ADR 027 #132 — Bedrock image-inference visual QA (ADVISORY, never fails the pipeline).

Captures a curated set of chart-bearing panels — NOT every screenshot in the full browser/
viewport matrix (cost containment, per ADR 027's "Bedrock image-inference visual QA"):
Mapa Competitivo and the B3 quotes panel on `/exec`, the threat×expansion quadrant on `/app`.
Each is evaluated by `amazon.nova-pro-v1:0` against a small visual-defect checklist
(`qa_pipeline/lib/vision.py`) and the verdict is uploaded to S3 for the report (#133).

This module NEVER raises — not on a failed/low-confidence verdict, not on a capture failure,
not on Bedrock being unavailable/denied. That is the deliberate, ADR-stated scope limit for
this layer at launch ("advisory, not blocking"); promoting any of it to a hard gate is an
explicit follow-up decision, not part of this check.
"""
from __future__ import annotations

import json
import time

from playwright.sync_api import sync_playwright

from qa_pipeline.lib import auth, config
from qa_pipeline.lib.browser import ARTIFACTS_BUCKET, CHROMIUM_LAUNCH_ARGS, upload_artifact
from qa_pipeline.lib.vision import VISION_MODEL, build_prompt, parse_verdict

# (panel key, path, locator kind, locator value). `section.band` is the wrapping element
# every `band()`-rendered panel on /exec gets (src/dashboard/site/v3/index.html); `/app`'s
# quadrant has its own stable id (src/dashboard/site/v2/app/index.html).
PANELS = [
    ("mapa_competitivo", "/exec", "band_text", "Mapa Competitivo · Ameaça × Momentum"),
    ("quotes", "/exec", "band_text", "Mercado · Cotações B3"),
    ("quadrant", "/app", "id", "quadrantBand"),
]


def _capture(page, kind: str, value: str) -> bytes:
    loc = page.locator(f"#{value}") if kind == "id" else page.locator("section.band").filter(has_text=value)
    loc.first.wait_for(state="visible", timeout=15_000)
    return loc.first.screenshot()


def run(event: dict) -> dict:
    from src.synth import bedrock_llm  # deferred: only this check needs the synth package

    persona = event.get("persona", "entry")
    creds = config.qa_credentials()["personas"][persona]
    user, pw = config.basic_auth()
    exec_url = f"{config.SITE_URL}/exec"
    run_id = event.get("run_id") or time.strftime("%Y%m%dT%H%M%SZ")
    findings: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROMIUM_LAUNCH_ARGS)
        ctx = browser.new_context(http_credentials={"username": user, "password": pw})
        page = ctx.new_page()
        auth.login_with_password(page, exec_url, creds["username"], creds["password"])

        current_url = None
        for key, path, kind, value in PANELS:
            entry: dict = {"panel": key, "path": path}
            try:
                target = f"{config.SITE_URL}{path}"
                if current_url != target:
                    page.goto(target, wait_until="networkidle", timeout=30_000)
                    page.wait_for_timeout(1200)
                    current_url = target
                if key == "quotes":
                    page.wait_for_timeout(3000)  # let the async B3 ticker fill in first
                png_bytes = _capture(page, kind, value)
            except Exception as exc:  # noqa: BLE001 - advisory: record and move on
                entry.update({"captured": False, "error": str(exc)})
                findings.append(entry)
                continue

            upload_artifact(ARTIFACTS_BUCKET, f"{run_id}/vision/{key}.png", body=png_bytes)
            raw = bedrock_llm.converse(
                # Confirmed live (#132): 500 was too tight — a 3-item checklist with notes
                # regularly exceeds it, truncating the JSON before its closing brace (a
                # different failure mode than the trailing-garbage one parse_verdict already
                # tolerates; a genuinely truncated object can't be recovered by re-parsing).
                build_prompt(key), model_id=VISION_MODEL, images=[png_bytes], max_tokens=900,
            )
            entry.update({"captured": True, **parse_verdict(raw)})
            findings.append(entry)

        browser.close()

    result = {"ok": True, "run_id": run_id, "findings": findings}  # always ok: advisory layer
    upload_artifact(ARTIFACTS_BUCKET, f"{run_id}/vision-result.json", body=json.dumps(result).encode())
    return result
