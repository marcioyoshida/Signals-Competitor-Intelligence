"""ADR 027 #131 — resiliency: broken-link scan (hard gate) + network interception.

One continuous Chromium journey, ONE browser/context/page for the whole run (see
checks/routing.py's `README.md` note: this Lambda's `--single-process` Chromium crashes the
entire browser the instant a second `BrowserContext` is created — every check here reuses
the same page across navigations instead, which is the proven-safe pattern).

## Broken-link scan

There is no large `<a href="/...">` link graph to crawl on these dashboards (confirmed by
inspection: `/exec`'s nav is buttons/tabs with `data-officer` attributes, `/app`'s nav is
in-page `#hash` links, not real page loads) — the actual "internal link" surface is the set
of friendly routes CloudFront's viewer-request function rewrites
(`infra/app.py`'s `routes` map) plus the static assets (`<script src>`/`<link href>`) each
page requests. This scan visits every one of those routes and asserts every same-origin
request it makes (the page itself AND every script/stylesheet) returns < 400 — the one HARD
gate in this pipeline besides `pytest` itself, per ADR 027: a dead route or a 404'd script
is unambiguous, not a judgment call.

## Network interception

Simulates the three ADR 027 conditions (`page.route()`, not a real network toggle) against
the endpoints reachable from this persona's normal usage: `/api/feed` (offline / aborted,
and slow/high-latency) and `/api/ask` (aborted mid-request). In every case, asserts a
graceful message renders — never a blank board, and never an uncaught JS exception
(tracked via `page.on("pageerror", ...)` for the entire run, not just per-scenario).

NOT covered: `/api/register` — the self-registration form only appears for an
authenticated-but-unprovisioned Google identity, the same automation gap already documented
in `checks/smoke.py` (needs a dedicated Google test identity + a tenant-cleanup strategy,
neither exists yet).
"""
from __future__ import annotations

import json
import time

from playwright.sync_api import sync_playwright

from qa_pipeline.lib import auth, config
from qa_pipeline.lib.browser import ARTIFACTS_BUCKET, CHROMIUM_LAUNCH_ARGS, upload_artifact
from qa_pipeline.lib.checklist import Checklist

# infra/app.py's CloudFront viewer-request `routes` map (the short aliases it rewrites to
# `/v2/<name>/index.html`) plus the two paths handled by their own special-cased rules.
FRIENDLY_ROUTES = [
    "/exec", "/entry/", "/app", "/admin", "/newentry",
    "/adquirencia", "/fintech", "/seguros", "/wealth",
]


def run(event: dict) -> dict:
    persona = event.get("persona", "entry")
    creds = config.qa_credentials()["personas"][persona]
    user, pw = config.basic_auth()
    exec_url = f"{config.SITE_URL}/exec"
    run_id = event.get("run_id") or time.strftime("%Y%m%dT%H%M%SZ")
    c = Checklist()
    page_errors: list[str] = []
    screenshot_path = "/tmp/resilience.png"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROMIUM_LAUNCH_ARGS)
        ctx = browser.new_context(http_credentials={"username": user, "password": pw})
        page = ctx.new_page()
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        # Real login first (uninterrupted network) — the session backing every check below.
        auth.login_with_password(page, exec_url, creds["username"], creds["password"])

        # --- Broken-link scan (hard gate) --------------------------------------------
        # Scoped to the page's own navigation response and its STATIC assets (script/
        # stylesheet requests) — not every same-origin request. `/feed.json`/`/api/*` calls
        # are intentionally access-controlled (#122) and already have their own dedicated
        # pass/fail contract tested in checks/routing.py (e.g. an admin page loaded without
        # ?opkey= correctly 403s on /feed.json — that is not a broken link, it's the gate
        # working). Confirmed live: without this exclusion, visiting /admin bare flagged a
        # false positive on the intentional opkey-less 403.
        for path in FRIENDLY_ROUTES:
            responses: list[tuple[str, int]] = []

            def _record(resp, _origin=config.SITE_URL):
                if resp.url.startswith(_origin) and resp.request.resource_type in ("script", "stylesheet"):
                    responses.append((resp.url, resp.status))

            page.on("response", _record)
            try:
                nav_resp = page.goto(f"{config.SITE_URL}{path}", wait_until="networkidle", timeout=30_000)
                page.wait_for_timeout(300)
            finally:
                page.remove_listener("response", _record)

            nav_ok = bool(nav_resp) and nav_resp.status < 400
            broken = [(u, s) for u, s in responses if s >= 400]
            c.check(f"route_ok:{path}", nav_ok and not broken,
                    f"nav_status={nav_resp.status if nav_resp else None} broken_assets={broken}")

        # --- Network interception ------------------------------------------------------
        # 1. /api/feed offline/aborted -> a graceful message, not a blank board.
        page.unroute_all()
        page.route("**/api/feed", lambda route: route.abort("internetdisconnected"))
        page.goto(exec_url, wait_until="networkidle", timeout=30_000)
        page.wait_for_timeout(1000)
        board_text = page.evaluate("() => document.getElementById('board').textContent")
        c.check("feed_offline_shows_graceful_message", len(board_text.strip()) > 0,
                f"board_text={board_text[:120]!r}")

        # 2. /api/feed on a slow/high-latency connection -> still resolves, no hang/crash.
        page.unroute_all()
        page.route("**/api/feed", lambda route: (time.sleep(4), route.continue_())[1])
        t0 = time.time()
        page.goto(exec_url, wait_until="networkidle", timeout=30_000)
        page.wait_for_timeout(500)
        elapsed = time.time() - t0
        logged_in_after_slow = page.evaluate("() => Ctx.isLoggedIn()")
        c.check("feed_slow_connection_eventually_resolves", elapsed >= 4 and logged_in_after_slow,
                f"elapsed={elapsed:.1f}s logged_in={logged_in_after_slow}")

        # 3. /api/ask aborted mid-request -> the existing catch renders a graceful refusal.
        page.unroute_all()
        page.goto(exec_url, wait_until="networkidle", timeout=30_000)
        page.wait_for_timeout(800)
        page.route("**/api/ask", lambda route: route.abort("failed"))
        page.fill("#askInput", "O que mudou esta semana?")
        page.click("#askBtn")
        page.wait_for_timeout(1500)
        ask_answer = page.evaluate("() => document.getElementById('askAns').textContent")
        c.check("ask_network_failure_shows_graceful_refusal", "Falha de rede" in ask_answer,
                f"ask_answer={ask_answer[:120]!r}")

        page.unroute_all()
        c.check("no_uncaught_js_exceptions", len(page_errors) == 0, f"page_errors={page_errors}")

        page.screenshot(path=screenshot_path)
        browser.close()

    upload_artifact(ARTIFACTS_BUCKET, f"{run_id}/resilience.png", path=screenshot_path)
    result = {"ok": c.all_ok, "checks": c.items, "run_id": run_id}
    upload_artifact(ARTIFACTS_BUCKET, f"{run_id}/resilience-result.json", body=json.dumps(result).encode())
    c.raise_if_failed()
    return result
