"""#277: Onça's OWN sign-in form. The Cognito managed-login page can't be resized (32px inputs and a
16px checkbox on a phone), so /exec and every page that calls OncaCtx.login() sign in with a form
built by v2/context.js. Google/Apple keep going straight to the provider (identity_provider=).

Two layers:
  * static checks on the shipped JS/CSS/QA helper (hermetic, run in CI);
  * a headless-Chromium render of /exec with /api/session/* stubbed (touch phone + desktop, light +
    dark). It talks only to a 127.0.0.1 server it starts itself, but a browser needs sockets, so it
    carries the `network` marker (the root conftest's socket guard) and skips when Playwright's
    Chromium isn't installed.
"""
from __future__ import annotations

import base64
import http.server
import json
import re
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "src" / "dashboard" / "site"
CTX = (SITE / "v2" / "context.js").read_text(encoding="utf-8")
CSS = (SITE / "v2" / "app.css").read_text(encoding="utf-8")
FORM = CTX.split("OWN SIGN-IN FORM (#277)", 1)[1].split("SCOPED FEED LOADER", 1)[0]


# --------------------------------------------------------------------------------- static -------
def test_login_without_a_provider_opens_the_form_and_never_redirects_to_login():
    assert "if (!useIdp) { openSignIn(); return; }" in CTX
    assert 'useIdp ? "oauth2/authorize" : "login"' not in CTX     # the old two-way path choice is gone
    assert "/login?" not in CTX and "${AUTH.domain}/login" not in CTX
    # providers: straight to authorize?identity_provider=, PKCE + the Portuguese hint as before
    assert "identity_provider: useIdp" in CTX and "`${AUTH.domain}/oauth2/authorize?${params.toString()}`" in CTX
    assert 'lang: "pt-BR"' in CTX and 'code_challenge_method: "S256"' in CTX
    # app mode: the provider URL still goes to the shell; the password form is NOT handed over
    assert 'if (APP) { toShell({ type: "auth", url: authUrl }); return; }' in CTX
    assert "toShell" not in FORM
    # the deep-link / return-path logic is kept for the provider trip
    assert 'rememberReturnPath("login")' in CTX and 'sessionStorage.setItem("onca_return", back)' in CTX
    # Esc/focus/etc. live on the same exported object
    assert "login, openSignIn, logout" in CTX


def test_form_talks_to_the_session_broker_and_handles_every_reply():
    for route in ('"password"', '"new-password"', '"forgot"', '"reset"'):
        assert f"siCall({route}" in FORM, route
    assert "sessionPost(path, body)" in FORM
    for reply in ("NEW_PASSWORD_REQUIRED", "invalid_credentials", "too_many_attempts", "unsupported_challenge",
                  "weak_password", "bad_code"):
        assert reply in FORM, reply
    # a password success is handled exactly like the code exchange (one shared function)
    assert "await acceptSession(j);" in CTX and "await acceptSession(r);" in FORM
    assert "markDeviceSession(j.idle_days)" in CTX.split("async function acceptSession", 1)[1]
    assert "const SI_MIN_PW = 12;" in FORM and "siPwLen(pw) < SI_MIN_PW" in FORM   # checked before calling


def test_form_copy_steps_and_accessibility_attributes():
    for text in ("Entrar", "Esqueceu a senha?", "Continuar com Google", "Continuar com Apple", "Defina sua senha",
                 "Enviamos um código, se o e-mail estiver cadastrado.", "E-mail ou senha incorretos.",
                 "Muitas tentativas. Aguarde alguns minutos.", "Política de privacidade"):
        assert text in FORM, text
    assert 'const SI_PRIVACY = "/docs/privacy.html";' in FORM
    for ac in ('"username"', '"current-password"', '"new-password"', '"one-time-code"'):
        assert ac in FORM, ac
    for attr in ('role="dialog"', 'aria-modal="true"', 'aria-labelledby="__siTitle"', 'role="alert"',
                 'role="status"', 'aria-invalid', 'aria-label="Mostrar senha"', "Ocultar senha"):
        assert attr in FORM, attr
    assert 'ev.key === "Escape"' in FORM and "opener.focus()" in FORM        # Esc closes, focus returns
    assert 'data-idp="${p}"' in FORM and "SignInWithApple" in FORM


def test_the_password_never_leaves_its_input():
    assert "console." not in FORM                      # nothing is logged
    assert "setItem" not in FORM and "localStorage" not in FORM          # nothing is stored by the form
    assert "i.value = \"\"" in FORM                    # inputs are wiped on every step change
    assert "SI.password" not in FORM and "SI.pw" not in FORM


def test_form_css_uses_tokens_and_touch_sizes():
    css = CSS.split("own sign-in form (#277)", 1)[1]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css.split("*/", 1)[1]), "ad-hoc hex in the sign-in CSS"
    assert "rgb(" not in css.split("*/", 1)[1]
    assert ".si input {" in css and "min-height: 48px" in css and "font-size: 16px" in css
    assert ".si__eye {" in css and "width: 44px; height: 44px" in css
    assert "@media (max-width: 600px)" in css and "min-height: 100dvh" in css      # phone = full-screen sheet
    assert "var(--on-primary)" in css and "var(--primary)" in css and "var(--t-crit)" in css


def test_every_page_that_calls_oncactx_login_loads_context_js():
    pages = [p for p in SITE.rglob("*.html") if "OncaCtx" in p.read_text(encoding="utf-8")
             or "Ctx.renderAuthBox" in p.read_text(encoding="utf-8")]
    assert pages, "expected context.js consumers"
    for p in pages:
        assert "/v2/context.js" in p.read_text(encoding="utf-8"), p


def test_only_the_known_pages_still_redirect_to_the_managed_login_page():
    """Only the legacy v1 page keeps its own managed-login PKCE sign-in. /v2/admin moved to the
    OncaCtx form too (ask.js no longer carries a built-in flow). Listed on purpose: a new page
    that builds `${AUTH.domain}/login` must not appear silently."""
    offenders = sorted(str(p.relative_to(SITE)) for p in list(SITE.rglob("*.html")) + list(SITE.rglob("*.js"))
                       if "${AUTH.domain}/login" in p.read_text(encoding="utf-8"))
    assert offenders == ["index.html"], offenders


def test_admin_signs_in_through_the_shared_form():
    """#277 follow-up: the operator page loads context.js BEFORE ask.js, and ask.js defaults to
    OncaCtx, so admin gets the same form, broker session and token as every client screen."""
    html = (SITE / "v2" / "admin" / "index.html").read_text(encoding="utf-8")
    assert 0 < html.index('src="/v2/context.js"') < html.index('src="/v2/ask.js"')
    ask = (SITE / "v2" / "ask.js").read_text(encoding="utf-8")
    assert "opts.auth || ctxAuth()" in ask and "global.OncaCtx" in ask
    for gone in ("/oauth2/token", "code_verifier", "VERIFIER_KEY", "auth.onssa.org"):
        assert gone not in ask, gone


def test_qa_login_fills_our_form_not_the_managed_page():
    src = (ROOT / "qa_pipeline" / "lib" / "auth.py").read_text(encoding="utf-8")
    body = src.split("def login_with_password", 1)[1].split("def seed_session_storage", 1)[0]
    assert "wait_for_url" not in body and "auth.onssa.org" not in body and "HOSTED_UI" not in body
    assert "SIGNIN_EMAIL" in body and "SIGNIN_PASSWORD" in body and "SIGNIN_SUBMIT" in body
    assert 'SIGNIN_EMAIL = "#si-email"' in src and 'SIGNIN_SUBMIT = "#si-submit"' in src
    assert 'HOSTED_UI_USERNAME = "input[name=username]:visible"' in src      # kept for managed-page scripts
    for script in ("oauth_mcp_e2e.py", "oauth_confidential_e2e.py"):          # still managed login, on purpose
        assert "signInSubmitButton" in (ROOT / "scripts" / script).read_text(encoding="utf-8")
    # the ids the QA helper fills exist in the form
    for ident in ('id="si-submit"', '"si-email"', '"si-pass"'):
        assert ident in FORM, ident


# --------------------------------------------------------------------------------- render -------
def _launch_or_skip():
    sync_api = pytest.importorskip("playwright.sync_api")
    pw = sync_api.sync_playwright().start()
    try:
        browser = pw.chromium.launch()
    except Exception as exc:  # noqa: BLE001 - no browser installed here
        pw.stop()
        pytest.skip(f"Chromium not available: {str(exc)[:80]}")
    return pw, browser


class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(SITE), **k)

    def translate_path(self, path):
        p = path.split("?")[0].split("#")[0]
        return super().translate_path("/v3/index.html" if p in ("/exec", "/exec/") else p)

    def log_message(self, *a):
        pass


def _jwt(email):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return enc({"alg": "none"}) + "." + enc({"sub": "u-" + email, "email": email, "exp": int(time.time()) + 3600}) + ".x"


@pytest.fixture(scope="module")
def site():
    pw, browser = _launch_or_skip()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"base": f"http://127.0.0.1:{srv.server_address[1]}", "browser": browser}
    srv.shutdown()
    browser.close()
    pw.stop()


def _open(site, *, touch, theme="light", app=False):
    """A signed-out /exec with /api/session/* stubbed. Returns (page, calls) — calls is every
    (route, body) the page POSTed to the broker."""
    vp = {"width": 390, "height": 844} if touch else {"width": 1440, "height": 900}
    ctx = site["browser"].new_context(viewport=vp, has_touch=touch, is_mobile=touch, color_scheme=theme)
    page = ctx.new_page()
    calls: list = []

    def session(route, request):
        path = request.url.split("/api/session/")[1]
        body = json.loads(request.post_data or "{}")
        calls.append((path, body))
        reply = lambda status, d: route.fulfill(status=status, content_type="application/json", body=json.dumps(d))  # noqa: E731
        email, pw = body.get("email", ""), body.get("password", "")
        if path == "refresh":
            return reply(401, {"session": "none"})
        if path == "password":
            if email.startswith("limit"): return reply(429, {"error": "too_many_attempts"})
            if email.startswith("down"): return reply(502, {"error": "upstream"})
            if email.startswith("invite"): return reply(200, {"challenge": "NEW_PASSWORD_REQUIRED", "session": "S1"})
            if pw == "correct-horse-battery": return reply(200, {"id_token": _jwt(email), "idle_days": 7})
            return reply(401, {"error": "invalid_credentials"})
        if path == "new-password":
            return reply(200, {"id_token": _jwt(body["email"]), "idle_days": 7})
        if path == "forgot":
            return reply(200, {"ok": True})
        if path == "reset":
            return reply(200, {"ok": True}) if body.get("code") == "123456" else reply(400, {"error": "bad_code"})
        return reply(404, {})

    page.route("**/api/session/**", session)
    page.route("**/api/feed**", lambda r, q: r.fulfill(
        status=403 if "authorization" in q.headers else 401, content_type="application/json", body="{}"))
    page.route("**/api/**", lambda r, q: r.fulfill(status=404, body="{}")
               if ("/api/session" not in q.url and "/api/feed" not in q.url) else r.fallback())
    page.route("https://auth.onssa.org/**", lambda r, q: r.fulfill(status=200, content_type="text/html", body="provider"))
    if app:
        page.add_init_script("window.__ONCA_APP__={redirectUri:'onca://auth',bridge:2}; window.__msgs=[];"
                             "window.ReactNativeWebView={postMessage:(m)=>window.__msgs.push(JSON.parse(m))};")
    page.goto(site["base"] + "/exec", wait_until="networkidle")
    page.evaluate(f"document.documentElement.setAttribute('data-theme','{theme}')")
    page.wait_for_selector("#__gateLogin", timeout=15_000)
    return page, calls


MEASURE = """() => Array.from(document.querySelectorAll('#__signin input, #__signin button, #__signin a[href]'))
  .filter((e) => e.offsetParent).map((e) => { const r = e.getBoundingClientRect();
    return [e.id || e.className, Math.round(r.height * 10) / 10, Math.round(r.width * 10) / 10,
            e.tagName === 'INPUT' ? parseFloat(getComputedStyle(e).fontSize) : 99]; })"""


@pytest.mark.network
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_control_is_a_44px_touch_target_on_a_phone(site, theme):
    page, _ = _open(site, touch=True, theme=theme)
    page.tap("#__gateLogin")
    page.wait_for_selector("#__signin .si__card")
    steps = {"signin": None, "forgot": "[data-to=forgot]"}
    for step, opener in steps.items():
        if opener:
            page.click(opener)
            page.wait_for_selector(f"[data-step={step}]:not([hidden])")
        rows = page.evaluate(MEASURE)
        assert rows, step
        small = [r for r in rows if r[1] < 44 or r[2] < 44 or r[3] < 16]
        assert small == [], f"{theme}/{step}: controls under 44px (or inputs under 16px): {small}"
    assert not page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
    page.context.close()


@pytest.mark.network
def test_phone_is_a_full_screen_sheet_and_desktop_a_centred_card(site):
    phone, _ = _open(site, touch=True)
    phone.tap("#__gateLogin")
    phone.wait_for_selector("#__signin .si__card")
    box = phone.eval_on_selector(".si__card", "e => { const r = e.getBoundingClientRect(); return [r.width, r.height]; }")
    assert box[0] == 390 and box[1] >= 844, box
    phone.context.close()
    desk, _ = _open(site, touch=False)
    desk.click("#__gateLogin")
    desk.wait_for_selector("#__signin .si__card")
    r = desk.eval_on_selector(".si__card", "e => { const r = e.getBoundingClientRect(); return [r.left, r.width, r.top]; }")
    assert r[1] <= 420 and abs((r[0] + r[1] / 2) - 720) < 2 and r[2] > 20, r
    desk.context.close()


@pytest.mark.network
def test_sign_in_errors_challenge_forgot_reset_and_no_cognito_page(site):
    page, calls = _open(site, touch=True)
    cognito: list[str] = []
    page.on("request", lambda r: cognito.append(r.url) if ("amazoncognito" in r.url or "auth.onssa.org" in r.url) else None)
    page.tap("#__gateLogin")
    page.wait_for_selector("#si-email")

    def attempt(email, pw):
        page.fill("#si-email", email)
        page.fill("#si-pass", pw)
        page.click("#si-submit")
        page.wait_for_function("document.querySelector('#si-msg-signin .si__txt')?.textContent.length > 0")
        return page.inner_text("#si-msg-signin")

    assert attempt("ana@banco.com.br", "wrong") == "E-mail ou senha incorretos."
    assert page.get_attribute("#si-msg-signin", "role") == "alert"
    assert attempt("limit@x.com", "x") == "Muitas tentativas. Aguarde alguns minutos."
    assert "Tente novamente" in attempt("down@x.com", "x")

    # forgot -> reset (the 12-character minimum is checked BEFORE calling) -> back to sign-in with a note
    page.click("[data-to=forgot]")
    page.click("[data-step=forgot] .si__go")
    page.wait_for_selector("[data-step=reset]:not([hidden])")
    page.wait_for_function("document.getElementById('si-note-reset').textContent.length > 0")
    assert page.inner_text("#si-note-reset") == "Enviamos um código, se o e-mail estiver cadastrado."
    n = len(calls)
    page.fill("#si-code", "123456")
    page.fill("#si-resetpw", "short")
    page.click("[data-step=reset] .si__go")
    assert len(calls) == n and "12" in page.inner_text("#si-msg-reset")
    page.fill("#si-resetpw", "a-long-new-password")
    page.click("[data-step=reset] .si__go")
    page.wait_for_selector("[data-step=signin]:not([hidden])")
    assert calls[-1] == ("reset", {"email": "down@x.com", "code": "123456", "new_password": "a-long-new-password"})
    assert page.input_value("#si-pass") == "" and page.input_value("#si-resetpw") == ""

    # an invited user: temporary password -> "Defina sua senha" -> signed in, the page reloads in place
    page.fill("#si-email", "invite@banco.com.br")
    page.fill("#si-pass", "Temp-pass-1!")
    page.click("#si-submit")
    page.wait_for_selector("[data-step=newpw]:not([hidden])")
    assert page.inner_text("#__siTitle") == "Defina sua senha"
    page.fill("#si-newpw", "twelve-chars!")
    with page.expect_navigation():
        page.click("[data-step=newpw] .si__go")
    assert calls[-1][0] == "new-password" and calls[-1][1]["session"] == "S1"
    assert page.evaluate("!!sessionStorage.getItem('onca_id_token')") and page.evaluate("!document.getElementById('__signin')")
    assert cognito == [], f"the password path must not touch Cognito: {cognito}"
    page.context.close()


@pytest.mark.network
def test_password_sign_in_keeps_the_password_out_of_storage_and_esc_restores_focus(site):
    page, calls = _open(site, touch=False)
    page.click("#__gateLogin")
    page.wait_for_selector("#__signin")
    page.keyboard.press("Escape")
    assert page.evaluate("!document.getElementById('__signin')")
    assert page.evaluate("document.activeElement && document.activeElement.id") == "__gateLogin"
    page.click("#__gateLogin")
    page.fill("#si-email", "ok@banco.com.br")
    page.fill("#si-pass", "correct-horse-battery")
    with page.expect_navigation():
        page.press("#si-pass", "Enter")
    assert calls[-1] == ("password", {"email": "ok@banco.com.br", "password": "correct-horse-battery"})
    dump = page.evaluate("JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)])")
    assert "correct-horse-battery" not in dump
    page.context.close()


@pytest.mark.network
def test_providers_go_straight_to_authorize_and_the_app_hands_them_to_the_shell(site):
    web, _ = _open(site, touch=False)
    web.click("#__gateLogin")
    web.click("[data-idp=Google]")
    web.wait_for_url("https://auth.onssa.org/**")
    url = web.url
    assert url.startswith("https://auth.onssa.org/oauth2/authorize?") and "identity_provider=Google" in url
    assert "lang=pt-BR" in url and "code_challenge_method=S256" in url
    web.context.close()

    app, _ = _open(site, touch=True, app=True)
    app.tap("#__gateLogin")
    app.wait_for_selector("#__signin")
    assert app.evaluate("window.__msgs.length") == 0          # "Entrar" opens the form, no shell hand-off
    box = app.locator("[data-idp=SignInWithApple]").bounding_box()
    app.touchscreen.tap(box["x"] + 20, box["y"] + 20)
    app.wait_for_timeout(250)
    app.touchscreen.tap(box["x"] + 20, box["y"] + 20)          # a second tap must not open the browser twice
    app.wait_for_timeout(250)
    msgs = app.evaluate("window.__msgs")
    assert len(msgs) == 1 and msgs[0]["type"] == "auth", msgs
    assert "/oauth2/authorize?" in msgs[0]["url"] and "identity_provider=SignInWithApple" in msgs[0]["url"]
    assert "redirect_uri=onca%3A%2F%2Fauth" in msgs[0]["url"]
    app.context.close()
