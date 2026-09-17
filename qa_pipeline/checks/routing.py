"""ADR 027 #130 — deep-linking, dynamic routing, browser history.

Every check below runs in its OWN full `browser.launch()`/`browser.close()` cycle, never a
second context on a shared browser instance. That's a hard requirement in this Lambda
environment, not a style choice — confirmed live building this: Chromium under
`--no-sandbox --single-process --no-zygote` (required for Chromium to survive Lambda's
process-fork restrictions at all, see qa_pipeline/README.md) crashes the ENTIRE browser
process the instant a second `BrowserContext` is created, whether or not the first one was
closed first. Each check here genuinely needs an isolated session anyway (a fresh
sessionStorage seed, or deliberately NO session, or a different query string) — the
per-check relaunch cost (a few seconds each) is the honest price of that isolation, not
overhead to optimize away.

All checks run against `/exec`; `/app` uses the identical `activeSlug()`/hash pattern (ADR
027 context) so is not duplicated here.

NOT covered here: opening an external link in a new tab (ADR 027's own text: "e.g. terms of
service opening in a new tab") — `/exec` has no `target="_blank"` link today to exercise;
nothing to fabricate. Revisit once one exists.
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager

from playwright.sync_api import sync_playwright

from qa_pipeline.lib import auth, config
from qa_pipeline.lib.browser import ARTIFACTS_BUCKET, CHROMIUM_LAUNCH_ARGS, upload_artifact
from qa_pipeline.lib.checklist import Checklist


@contextmanager
def _fresh_page(user: str, pw: str):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROMIUM_LAUNCH_ARGS)
        ctx = browser.new_context(http_credentials={"username": user, "password": pw})
        try:
            yield ctx.new_page()
        finally:
            browser.close()


def run(event: dict) -> dict:
    persona = event.get("persona", "entry")
    creds = config.qa_credentials()["personas"][persona]
    user, pw = config.basic_auth()
    exec_url = f"{config.SITE_URL}/exec"
    run_id = event.get("run_id") or time.strftime("%Y%m%dT%H%M%SZ")
    c = Checklist()
    screenshot_path = "/tmp/routing.png"

    # One real login, its captured sessionStorage reused by every check below that needs a
    # session (mirrors #128's login-once-fan-out pattern) — its OWN browser lifecycle.
    with _fresh_page(user, pw) as page:
        session_storage = auth.login_with_password(page, exec_url, creds["username"], creds["password"])

    # 1. A licensed hash survives a reload.
    with _fresh_page(user, pw) as page:
        auth.seed_session_storage(page.context, session_storage)
        page.goto(f"{exec_url}#agri-funds", wait_until="networkidle")
        page.wait_for_timeout(600)
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(600)
        sector = page.evaluate("() => SECTOR")
        c.check("licensed_hash_survives_reload", sector == "agri-funds", f"SECTOR={sector!r}")

    # 2. An unlicensed/garbage hash falls back to the licensed default, not unscoped data.
    with _fresh_page(user, pw) as page:
        auth.seed_session_storage(page.context, session_storage)
        page.goto(f"{exec_url}#not-a-real-sector-xyz", wait_until="networkidle")
        page.wait_for_timeout(600)
        sector = page.evaluate("() => SECTOR")
        c.check("unlicensed_hash_falls_back", sector == "__all__", f"SECTOR={sector!r}")

    # 3. An unauthenticated deep link renders the honest gate, never a stale/empty board.
    with _fresh_page(user, pw) as page:
        page.goto(exec_url, wait_until="networkidle")
        page.wait_for_timeout(600)
        gate_present = page.evaluate("() => !!document.querySelector('#__gateLogin')")
        c.check("unauthenticated_deep_link_shows_gate", gate_present, f"gate_present={gate_present}")

    # 4. ?admin=1&opkey=<correct> unlocks the full unscoped preview.
    opkey = config.operator_secret()
    with _fresh_page(user, pw) as page:
        page.goto(f"{exec_url}?admin=1&opkey={opkey}", wait_until="networkidle")
        page.wait_for_timeout(800)
        licensed_count = page.evaluate("() => LICENSED.length")
        c.check("admin_bypass_correct_opkey_unlocks_full_feed", licensed_count > 1,
                f"LICENSED.length={licensed_count} (expected >1, entry persona alone licenses only 1)")

    # 5. ?admin=1&opkey=<wrong> is rejected, not silently downgraded to a partial view.
    with _fresh_page(user, pw) as page:
        page.goto(f"{exec_url}?admin=1&opkey=definitely-wrong", wait_until="networkidle")
        page.wait_for_timeout(800)
        board_text = page.evaluate("() => document.getElementById('board').textContent")
        c.check("admin_bypass_wrong_opkey_rejected", "Feed indisponível" in board_text,
                f"board_text={board_text[:120]!r}")

    # 6. Browser back/forward across hash changes keeps SECTOR in sync with the URL.
    with _fresh_page(user, pw) as page:
        auth.seed_session_storage(page.context, session_storage)
        page.goto(f"{exec_url}#__all__", wait_until="networkidle")
        page.wait_for_timeout(500)
        page.evaluate("() => { location.hash = '#agri-funds'; }")
        page.wait_for_timeout(400)
        sector_after_nav = page.evaluate("() => SECTOR")
        page.go_back()
        page.wait_for_timeout(400)
        sector_after_back = page.evaluate("() => SECTOR")
        page.go_forward()
        page.wait_for_timeout(400)
        sector_after_forward = page.evaluate("() => SECTOR")
        c.check(
            "history_back_forward_stays_in_sync",
            sector_after_nav == "agri-funds" and sector_after_back == "__all__"
            and sector_after_forward == "agri-funds",
            f"after_nav={sector_after_nav!r} after_back={sector_after_back!r} "
            f"after_forward={sector_after_forward!r}",
        )
        page.screenshot(path=screenshot_path)

    upload_artifact(ARTIFACTS_BUCKET, f"{run_id}/routing.png", path=screenshot_path)
    result = {"ok": c.all_ok, "checks": c.items, "run_id": run_id}
    upload_artifact(ARTIFACTS_BUCKET, f"{run_id}/routing-result.json", body=json.dumps(result).encode())
    c.raise_if_failed()
    return result
