"""Playwright login helpers for the ADR 027 QA personas (#126).

Onça's session token lives in `sessionStorage` under the key `onca_id_token`
(`src/dashboard/site/v2/context.js`), NOT `localStorage`. Since #161/#164 the code exchange runs
server-side (`/api/session/exchange`) and the REFRESH token is an HttpOnly cookie scoped to
`/api/session` — a page with no ID token silently gets one from `/api/session/refresh`.
Playwright's own `BrowserContext.storage_state()` only captures cookies + localStorage,
so it does NOT carry this token — the widely-used "storageState reuse" pattern does not
apply here out of the box. This module captures sessionStorage explicitly after login and
re-injects it into a fresh context via `add_init_script`, which is the correct Playwright
idiom for sessionStorage-backed auth (confirmed working live 2026-09-16, both QA personas).
"""
from __future__ import annotations

import json
from typing import Any

from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

SESSION_TOKEN_KEY = "onca_id_token"
LOGIN_BUTTON = "#__loginBtn"
# The signed-out board's own "Entrar" (Ctx.mountGate). On a phone (d3186b9) the header
# #__loginBtn is display:none while the auth gate shows (`body[data-gate="auth"] #authBox`) —
# the gate's button is the one action a user has. Click whichever "Entrar" is on screen.
GATE_LOGIN_BUTTON = "#__gateLogin"
VISIBLE_LOGIN = f"{LOGIN_BUTTON}:visible, {GATE_LOGIN_BUTTON}:visible"
# #277: the client sign-in path is Onça's OWN form (context.js openSignIn) — no Cognito page, no
# navigation to auth.onssa.org. The e-mail/password go to POST /api/session/password, which sets the
# same HttpOnly refresh cookie as the code exchange and returns the ID token; the page then reloads
# in place, signed in.
SIGNIN_EMAIL = "#si-email"
SIGNIN_PASSWORD = "#si-pass"
SIGNIN_SUBMIT = "#si-submit"
SIGNIN_ERROR = "#si-msg-signin .si__txt"
SIGNIN_FORM = "#__signin"
# Cognito MANAGED LOGIN selectors: NOT used by login_with_password any more. Kept for anything that
# still drives the managed page (the OAuth facade / MCP e2e scripts, which sign in through it on
# purpose). They also match the classic Hosted UI, which renders a desktop and a mobile copy of the
# form with only one visible — hence `:visible` (a bare match picks the hidden copy).
HOSTED_UI_USERNAME = "input[name=username]:visible"
HOSTED_UI_PASSWORD = "input[name=password]:visible"
HOSTED_UI_SUBMIT = 'input[name="signInSubmitButton"]:visible, button[type=submit]:visible'


def login_with_password(page: Page, url: str, username: str, password: str) -> dict[str, Any]:
    """Sign in through Onça's own sign-in form (#277) — the exact button/fields a human uses
    (`context.js`'s `login()` -> `openSignIn()`), not a shortcut through the broker. There is no
    navigation to the Cognito domain: the form posts to `/api/session/password`, the page
    reloads in place with the ID token in sessionStorage, and the signed-in board renders.
    Returns the captured sessionStorage dict; save it with `dump_session_storage`/reuse it with
    `seed_session_storage`.

    Raises RuntimeError if the form did not land a token — a silent auth failure here (e.g. the
    form markup changed, wrong credentials) must fail the calling test loudly, not proceed as if
    logged in. The error text shown by the form is included; the password never is.
    """
    page.goto(url, wait_until="networkidle")
    page.locator(VISIBLE_LOGIN).first.click(timeout=10_000)
    page.wait_for_selector(SIGNIN_EMAIL, state="visible", timeout=10_000)
    page.fill(SIGNIN_EMAIL, username)
    page.fill(SIGNIN_PASSWORD, password)
    page.click(SIGNIN_SUBMIT)
    try:
        # success: the token is stored, the page reloads and the sheet is gone
        page.wait_for_function(
            "([k, f]) => !!sessionStorage.getItem(k) && !document.querySelector(f)",
            arg=[SESSION_TOKEN_KEY, SIGNIN_FORM], timeout=25_000)
    except PlaywrightTimeoutError:
        err = page.locator(SIGNIN_ERROR)
        detail = err.first.inner_text() if err.count() else "no answer from /api/session/password"
        raise RuntimeError(f"login to {url} failed in the sign-in form: {detail}") from None
    # the signed-in board: the reloaded page has fetched its feed and no "Entrar" is on screen
    page.wait_for_load_state("networkidle", timeout=30_000)
    page.locator(VISIBLE_LOGIN).first.wait_for(state="hidden", timeout=20_000)
    storage = json.loads(page.evaluate("() => JSON.stringify(sessionStorage)"))
    if SESSION_TOKEN_KEY not in storage:
        raise RuntimeError(f"login to {url} did not produce a {SESSION_TOKEN_KEY!r} token")
    return storage


def seed_session_storage(context: BrowserContext, storage: dict[str, Any]) -> None:
    """Re-inject a previously captured sessionStorage dict into a FRESH context, before any
    navigation — the sessionStorage equivalent of Playwright's own cookie/localStorage
    `storage_state()` reuse (which does not cover sessionStorage). Call this once per new
    context, then navigate; every downstream page in that context sees the token as if it
    had just logged in itself, without repeating the Hosted UI round-trip."""
    script = "".join(
        f"sessionStorage.setItem({json.dumps(k)}, {json.dumps(v)});"
        for k, v in storage.items()
    )
    context.add_init_script(script)
