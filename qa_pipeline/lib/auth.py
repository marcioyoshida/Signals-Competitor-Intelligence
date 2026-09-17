"""Playwright login helpers for the ADR 027 QA personas (#126).

Onça's session token lives in `sessionStorage` under the key `onca_id_token`
(`src/dashboard/site/v2/context.js`), NOT `localStorage` and NOT a cookie.
Playwright's own `BrowserContext.storage_state()` only captures cookies + localStorage,
so it does NOT carry this token — the widely-used "storageState reuse" pattern does not
apply here out of the box. This module captures sessionStorage explicitly after login and
re-injects it into a fresh context via `add_init_script`, which is the correct Playwright
idiom for sessionStorage-backed auth (confirmed working live 2026-09-16, both QA personas).
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Page

SESSION_TOKEN_KEY = "onca_id_token"
LOGIN_BUTTON = "#__loginBtn"
# Cognito's classic Hosted UI login page renders both a desktop and a mobile-responsive
# copy of the same form in the DOM; only one is visible at a time. `:visible` picks the
# one actually on screen — a bare `#signInFormUsername` resolves to 2 elements and
# Playwright's auto-wait times out on the hidden one it picks first.
HOSTED_UI_USERNAME = "#signInFormUsername:visible"
HOSTED_UI_PASSWORD = "#signInFormPassword:visible"
HOSTED_UI_SUBMIT = 'input[name="signInSubmitButton"]:visible'


def login_with_password(page: Page, url: str, username: str, password: str) -> dict[str, Any]:
    """Drive a REAL Cognito Hosted UI password login round-trip (Authorization Code + PKCE)
    against `url` — the exact same button/flow a human uses (`context.js`'s `login()`), not
    a shortcut through the token endpoint. Returns the captured sessionStorage dict; save it
    with `dump_session_storage`/reuse it with `seed_session_storage`.

    Raises RuntimeError if the round-trip did not land a token — a silent auth failure here
    (e.g. Hosted UI markup changed) must fail the calling test loudly, not proceed as if
    logged in.
    """
    page.goto(url, wait_until="networkidle")
    page.click(LOGIN_BUTTON, timeout=10_000)
    page.wait_for_url("**/login**", timeout=15_000)
    page.fill(HOSTED_UI_USERNAME, username)
    page.fill(HOSTED_UI_PASSWORD, password)
    page.click(HOSTED_UI_SUBMIT)
    base = urlsplit(url)
    callback_prefix = f"{base.scheme}://{base.netloc}{base.path}"
    page.wait_for_url(f"{callback_prefix}**", timeout=20_000)
    # The app's own JS exchanges ?code=... for a token asynchronously after the redirect
    # lands; wait for that exchange to finish (query string cleared) rather than assuming
    # the redirect alone means the token is already in sessionStorage.
    page.wait_for_function("() => !location.search.includes('code=')", timeout=15_000)
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
