#!/usr/bin/env python3
"""#126: authenticate as a QA persona against the LIVE site and save its session state.

    python qa_pipeline/tasks/login_smoke.py entry
    python qa_pipeline/tasks/login_smoke.py admin --out /tmp/qa-admin-session.json

Live-verified 2026-09-16 against both personas end-to-end (Hosted UI login -> ID token ->
GET /api/feed with that token returns 200, correctly scoped). This is also the task
`OncaQaPipeline` (#127) runs once per persona per pipeline execution, uploading the result
to S3 instead of /tmp, so every downstream Map-shard Lambda can reuse it without
re-authenticating.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from playwright.sync_api import sync_playwright  # noqa: E402

from qa_pipeline.lib import auth, config  # noqa: E402


def run(persona: str, url: str) -> dict:
    creds = config.qa_credentials()["personas"][persona]
    user, pw = config.basic_auth()
    with sync_playwright() as pw_ctx:
        browser = pw_ctx.chromium.launch(headless=True)
        context = browser.new_context(http_credentials={"username": user, "password": pw})
        page = context.new_page()
        storage = auth.login_with_password(page, url, creds["username"], creds["password"])
        browser.close()
    return {"persona": persona, "username": creds["username"], "tenant_id": creds["tenant_id"],
            "tier": creds["tier"], "session_storage": storage}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("persona", choices=["entry", "admin"])
    p.add_argument("--out", default=None, help="local path to write the session JSON")
    p.add_argument("--url", default=None, help="defaults to $ONCA_QA_SITE_URL/exec")
    args = p.parse_args(argv)

    url = args.url or f"{config.SITE_URL}/exec"
    result = run(args.persona, url)

    out = args.out or f"/tmp/qa-{args.persona}-session.json"
    Path(out).write_text(json.dumps(result))
    print(f"OK  {result['persona']}  {result['username']}  tenant={result['tenant_id']}  -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
