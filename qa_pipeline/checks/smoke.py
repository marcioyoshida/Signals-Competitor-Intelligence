"""ADR 027 #129 — smoke & critical-path navigation.

One continuous Chromium journey, all in ONE page/context/browser lifecycle (see the note in
the reachability section below for why that's a hard requirement, not a style choice) on
`/exec` (login -> persistence -> core action -> officer/theme/drawer interactions ->
cross-page session continuity on `/app` -> logout -> /entry/ + /admin reachability). Every
check runs even if an earlier one fails (`Checklist`) so one broken selector doesn't hide
the rest.

NOT covered here (documented gaps, not silent omissions):
  - "Continue with Google" -> lazy Entry self-registration. Automating this safely needs a
    dedicated Google test identity PLUS a way to reset it back to "never registered" (or
    accept an ever-growing set of `entry-<local>-<hex>` tenant rows in onca-tenant-config
    every run) — neither exists yet. See qa_pipeline/README.md.
  - A real login FROM `/app` itself: the Cognito client's CallbackURLs only register
    `/exec` (and `/`), not `/app` — `Ctx.login()` builds `redirect_uri` from
    `location.pathname`, so clicking "Entrar" while ON `/app` would hit an OAuth
    `redirect_uri_mismatch` today. This smoke test sidesteps it by logging in on `/exec`
    (a registered callback) and then navigating to `/app` in the SAME browser context —
    sessionStorage is shared across same-origin pages in one tab, so this still proves
    `/app` honors an existing session correctly. Whether `/app` ought to be a registered
    callback too (for a design partner who bookmarks it directly) is a real open question,
    flagged here rather than fixed unilaterally — it is a production Cognito client change.
"""
from __future__ import annotations

import json
import time

from playwright.sync_api import sync_playwright

from qa_pipeline.lib import auth, config
from qa_pipeline.lib.browser import ARTIFACTS_BUCKET, CHROMIUM_LAUNCH_ARGS, upload_artifact
from qa_pipeline.lib.checklist import Checklist


def run(event: dict) -> dict:
    persona = event.get("persona", "entry")
    creds = config.qa_credentials()["personas"][persona]
    user, pw = config.basic_auth()
    exec_url = f"{config.SITE_URL}/exec"
    run_id = event.get("run_id") or time.strftime("%Y%m%dT%H%M%SZ")
    c = Checklist()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROMIUM_LAUNCH_ARGS)
        ctx = browser.new_context(http_credentials={"username": user, "password": pw})
        page = ctx.new_page()

        # 1. Fresh Hosted UI password login (the real round-trip, not a seeded session).
        try:
            auth.login_with_password(page, exec_url, creds["username"], creds["password"])
            c.check("login", True)
        except Exception as exc:  # noqa: BLE001 - record and keep going
            c.check("login", False, str(exc))

        # 2. Session persistence across a hard reload.
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(800)
        logged_in = page.evaluate("() => Ctx.isLoggedIn()")
        gate_showing = page.evaluate("() => !!document.querySelector('#__gateLogin')")
        c.check("session_persists_across_reload", logged_in and not gate_showing,
                f"isLoggedIn={logged_in} gate_showing={gate_showing}")

        # 3. Core action: officer board renders with the expected 4 officer tabs.
        tab_count = page.evaluate("() => document.querySelectorAll('#officerTabs a').length")
        c.check("officer_tabs_render", tab_count == 4, f"found {tab_count}, expected 4")

        # 4. Officer tab switch actually routes.
        page.click('#officerTabs a[data-officer="cro"]')
        page.wait_for_timeout(300)
        officer = page.evaluate("() => OFFICER")
        c.check("officer_tab_switch_routes", officer == "cro", f"OFFICER={officer!r}")

        # 5. Theme toggle actually flips the theme attribute.
        theme_before = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
        page.click("#themeBtn")
        page.wait_for_timeout(200)
        theme_after = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
        c.check("theme_toggle_flips_attribute", theme_before != theme_after,
                f"before={theme_before!r} after={theme_after!r}")

        # 6. Inspection drawer actually becomes visible on open, not just aria-flagged
        # (ADR 027 #129 caught this live: openDrawer()/closeDrawer() used to toggle a class
        # with no matching CSS rule — aria-hidden flipped, the drawer stayed off-screen).
        page.evaluate("() => inspect('qa-smoke-dummy-id')")
        page.wait_for_timeout(400)
        drawer_open = page.evaluate("""() => {
            const r = document.getElementById('drawer').getBoundingClientRect();
            return r.left < window.innerWidth - 1;
        }""")
        aria_open = page.evaluate("() => document.getElementById('drawer').getAttribute('aria-hidden') === 'false'")
        c.check("drawer_opens_visibly", drawer_open and aria_open,
                f"visibly_on_screen={drawer_open} aria_hidden_false={aria_open}")

        page.click("#drClose")
        page.wait_for_timeout(400)
        aria_closed = page.evaluate("() => document.getElementById('drawer').getAttribute('aria-hidden') === 'true'")
        c.check("drawer_closes", aria_closed, f"aria_hidden_true={aria_closed}")

        # 7. Cross-page session continuity: /app in the SAME context, no fresh login.
        page.goto(f"{config.SITE_URL}/app", wait_until="networkidle")
        page.wait_for_timeout(800)
        app_gate_showing = page.evaluate("() => !!document.querySelector('#__gateLogin')")
        app_nav_count = page.evaluate("() => document.querySelectorAll('#ctxnav a').length")
        c.check("app_honors_existing_session", not app_gate_showing and app_nav_count > 0,
                f"gate_showing={app_gate_showing} nav_count={app_nav_count}")

        # 8. Logout actually clears the session.
        page.goto(exec_url, wait_until="networkidle")
        page.wait_for_timeout(500)
        page.click("#__logoutBtn")
        page.wait_for_timeout(1500)
        still_logged_in = page.evaluate("() => Ctx.isLoggedIn()")
        c.check("logout_clears_session", not still_logged_in, f"isLoggedIn={still_logged_in}")

        # 9. Lightweight reachability for /entry/ and /admin (basic-auth only, no Cognito
        # needed for either) — loads without error, right page. Reuses the SAME page/context
        # rather than opening a new one: confirmed live that this Lambda's Chromium
        # (--single-process, required to survive Lambda's process-fork restrictions — see
        # qa_pipeline/README.md) cannot survive a SECOND browser context in one launch —
        # BrowserContext.new_page crashes the entire browser, not just that context, the
        # instant a second context is created. Every isolated scenario in this pipeline
        # must be its own full `browser.launch()`/`browser.close()`, never a second context
        # on a shared one (see checks/routing.py, which needs several isolated sessions).
        for path, expected_title in (("/entry/", "Onça — Entry Portal"), ("/admin", "Onça · Admin — Sala de Guerra")):
            page.goto(f"{config.SITE_URL}{path}", wait_until="networkidle", timeout=30_000)
            title = page.title()
            c.check(f"reachable{path}", title == expected_title, f"title={title!r}")

        screenshot_path = "/tmp/smoke.png"
        page.screenshot(path=screenshot_path)
        browser.close()

    upload_artifact(ARTIFACTS_BUCKET, f"{run_id}/smoke.png", path=screenshot_path)
    result = {"ok": c.all_ok, "checks": c.items, "run_id": run_id}
    upload_artifact(ARTIFACTS_BUCKET, f"{run_id}/smoke-result.json", body=json.dumps(result).encode())
    c.raise_if_failed()
    return result
