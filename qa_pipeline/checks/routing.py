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
    # Sector hashes, the selector and the officer suite only exist above the Entry tier (ADR 016,
    # ed24dcf: an Entry tenant's /exec is the Entry board, which never reads SECTOR) — the hash
    # checks run as the sovereign persona; the Entry persona gets its own boundary check (7).
    event_persona = event.get("persona", "entry")
    persona = config.officer_suite_persona(event)
    creds = config.qa_credentials()["personas"][persona]
    user, pw = config.basic_auth()
    exec_url = f"{config.SITE_URL}/exec"
    run_id = event.get("run_id") or time.strftime("%Y%m%dT%H%M%SZ")
    c = Checklist()
    screenshot_path = "/tmp/routing.png"

    # One real login, its captured sessionStorage reused by every check below that needs a
    # session (mirrors #128's login-once-fan-out pattern) — its OWN browser lifecycle.
    # The licensed sector is read from the persona's own scoped feed (LICENSED), not hard-coded,
    # so re-provisioning the QA tenant doesn't silently turn check 1 into a fallback test.
    with _fresh_page(user, pw) as page:
        session_storage = auth.login_with_password(page, exec_url, creds["username"], creds["password"])
        page.wait_for_function("() => typeof LICENSED !== 'undefined' && LICENSED.length > 0", timeout=20_000)
        licensed = page.evaluate("() => LICENSED.slice()")
    sector_ok = licensed[0]

    # 1. A licensed hash survives a reload.
    with _fresh_page(user, pw) as page:
        auth.seed_session_storage(page.context, session_storage)
        page.goto(f"{exec_url}#{sector_ok}", wait_until="networkidle")
        page.wait_for_timeout(600)
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(600)
        sector = page.evaluate("() => SECTOR")
        selected = page.evaluate("() => document.getElementById('sectorSel').value")
        c.check("licensed_hash_survives_reload", sector == sector_ok and selected == sector_ok,
                f"SECTOR={sector!r} sectorSel={selected!r} expected={sector_ok!r} (persona={persona})")

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
        c.check("admin_bypass_correct_opkey_unlocks_full_feed", licensed_count > len(licensed),
                f"LICENSED.length={licensed_count} (expected more than the {len(licensed)} sectors "
                f"the {persona} persona licenses — no session is seeded here)")

    # 5. ?admin=1&opkey=<wrong> is rejected, not silently downgraded to a partial view.
    with _fresh_page(user, pw) as page:
        page.goto(f"{exec_url}?admin=1&opkey=definitely-wrong", wait_until="networkidle")
        page.wait_for_timeout(800)
        # 21e15e5 (#234) gave the operator path its own copy ("Acesso de operador indisponível
        # — O link de operador é inválido ou expirou.") instead of the generic "Feed indisponível".
        # Rejected = that gate is shown AND no feed was loaded behind it (not a partial view).
        board_text = page.evaluate("() => document.getElementById('board').textContent")
        loaded = page.evaluate("() => LICENSED.length")
        gated = page.evaluate("() => document.body.classList.contains('is-gated')")
        c.check("admin_bypass_wrong_opkey_rejected",
                "Acesso de operador indisponível" in board_text and loaded == 0 and gated,
                f"board_text={' '.join(board_text.split())[:120]!r} LICENSED.length={loaded} is_gated={gated}")

    # 6. Browser back/forward across hash changes keeps SECTOR in sync with the URL.
    with _fresh_page(user, pw) as page:
        auth.seed_session_storage(page.context, session_storage)
        page.goto(f"{exec_url}#__all__", wait_until="networkidle")
        page.wait_for_timeout(500)
        page.evaluate("(s) => { location.hash = '#' + s; }", sector_ok)
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
            sector_after_nav == sector_ok and sector_after_back == "__all__"
            and sector_after_forward == sector_ok,
            f"after_nav={sector_after_nav!r} after_back={sector_after_back!r} "
            f"after_forward={sector_after_forward!r}",
        )
        page.screenshot(path=screenshot_path)

    # 7. The Entry tier's boundary (ADR 016): an Entry tenant gets the Entry board — no officer
    # tabs, Ask or sector selector — and its licensed module is named on it. Runs as the event's
    # own Entry persona (the one the state machine sends), with its own login.
    if event_persona in config.ENTRY_TIER_PERSONAS:
        ecreds = config.qa_credentials()["personas"][event_persona]
        with _fresh_page(user, pw) as page:
            try:
                auth.login_with_password(page, exec_url, ecreds["username"], ecreds["password"])
                page.wait_for_function("() => typeof DATA !== 'undefined' && !!DATA.tier", timeout=20_000)
                tier = page.evaluate("() => DATA.tier")
                suite_shown = [sel for sel in ("#officerTabs", "#askPanel", "#sectorSel")
                               if page.is_visible(sel)]
                title = page.evaluate("() => document.getElementById('pgTitle').textContent")
                c.check("entry_tier_gets_entry_board", tier == "entry" and not suite_shown
                        and title.startswith("Plano Entry"),
                        f"tier={tier!r} officer_suite_visible={suite_shown} title={title!r}")
            except Exception as exc:  # noqa: BLE001 - record and keep going
                c.check("entry_tier_gets_entry_board", False, str(exc))

    upload_artifact(ARTIFACTS_BUCKET, f"{run_id}/routing.png", path=screenshot_path)
    result = {"ok": c.all_ok, "checks": c.items, "run_id": run_id}
    upload_artifact(ARTIFACTS_BUCKET, f"{run_id}/routing-result.json", body=json.dumps(result).encode())
    c.raise_if_failed()
    return result
