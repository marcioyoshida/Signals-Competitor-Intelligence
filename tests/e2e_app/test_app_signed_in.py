"""Layer 1, signed in (#259): the QA persona's /exec in app mode — tab bar, identity, account sheet,
cards, text hygiene, offline launch and sign-out through the shell.

The app signs in through ASWebAuthenticationSession / Custom Tabs, which a browser driver cannot
reach. So the session is made the way the web does it — the own sign-in form (#277, no Cognito page) in this same browser
context WITHOUT the app flag  — and then the page is reloaded WITH the
app flag and WITHOUT the ID token, so only the session broker's refresh cookie keeps it signed in.
That is the state the app is in after its own exchange (same /exec, same broker, same cookie).

Tests in this module share one session and run in file order; sign-out is last.
"""
from __future__ import annotations

import re
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from tests.e2e_app.harness import (
    APP_INIT, AUTH_ORIGIN, BASE_URL, Watch, attach, new_context, shell_messages, shot,
)

pytestmark = pytest.mark.timeout(300)

OFFICERS = [("cso", "Estratégia"), ("cro", "Regulatório"), ("cco", "Compliance"), ("cpo", "Produto")]
BAD_TEXT = re.compile(r"(?<![\w-])(None|NaN|undefined)(?![\w-])|#None")
PURCHASE = re.compile(r"Assin|R\$|pagamento|Upgrade|Comprar|Preço", re.I)
DATE = re.compile(
    r"\b\d{1,2}[ /.-](?:\d{1,2}|jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)[a-zç]*\.?[ /.-]\d{2,4}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2} de [a-zç]+ de \d{4}\b", re.I)


def _q(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def hosted_ui_login(page, live_auth, persona):
    """Web-mode sign-in through Onça's OWN form (#277): no Cognito page, no navigation — the form
    posts to /api/session/password and the page reloads signed in. (Name kept for the callers.)"""
    page.goto(f"{BASE_URL}/exec", wait_until="networkidle")
    page.wait_for_selector('body[data-gate="auth"]', timeout=30_000)
    page.get_by_role("button", name="Entrar").first.click()
    page.locator("#si-email").wait_for(state="visible", timeout=15_000)
    assert page.url.startswith(f"{BASE_URL}/exec"), f"the form must not navigate away: {page.url}"
    page.locator("#si-email").fill(persona["username"])
    page.locator("#si-pass").fill(persona["password"])
    page.locator("#si-submit").click()
    try:
        page.wait_for_function("!!sessionStorage.getItem('onca_id_token')", timeout=30_000)
    except Exception:
        err = page.locator("#si-msg-signin")
        raise AssertionError(f"sign-in form did not sign in "
                             f"({err.first.inner_text() if err.count() else page.url.split('?')[0]})")
    assert "amazoncognito.com" not in page.url and "auth.onssa.org" not in page.url, page.url


@pytest.fixture(scope="module")
def S(pw, engine, live_auth, qa_persona):
    ctx = new_context(pw, engine[1], app_mode=False)
    page = ctx.new_page()
    watch = Watch(page)
    t0 = time.monotonic()
    hosted_ui_login(page, live_auth, qa_persona)
    login_s = time.monotonic() - t0
    # From here on: app mode, and no ID token — the HttpOnly refresh cookie must carry the session.
    ctx.add_init_script(APP_INIT)
    page.evaluate("sessionStorage.removeItem('onca_id_token')")
    page.reload(wait_until="networkidle")
    page.wait_for_selector("body.is-authed", timeout=45_000)
    page.wait_for_function("document.querySelectorAll('#board *').length > 20", timeout=45_000)
    yield SimpleNamespace(ctx=ctx, page=page, watch=watch, persona=qa_persona, engine=engine[0],
                          live_auth=live_auth, login_s=login_s)
    ctx.close()


DRAWER_ON_SCREEN = """(() => { const d = document.getElementById('drawer'); if (!d.classList.contains('open')) return false;
  const r = d.getBoundingClientRect(), vw = document.documentElement.clientWidth, vh = innerHeight;
  return r.width > 0 && r.left >= -1 && r.right <= vw + 1 && r.top >= -1 && r.bottom <= vh + 1; })()"""


def _wait_drawer(page):
    """Open AND slid in (the sheet animates; a shot taken mid-transition shows the page)."""
    page.wait_for_function(DRAWER_ON_SCREEN, timeout=10_000)
    page.wait_for_timeout(150)


def _close_drawer(page):
    if page.evaluate("document.getElementById('drawer').classList.contains('open')"):
        page.locator("#drClose").tap()
        page.wait_for_function("!document.getElementById('drawer').classList.contains('open')")


def _goto_tab(page, key, label, view="hoje"):
    page.locator("#officerTabs a", has_text=label).tap()
    page.wait_for_function(f"location.hash.split('/')[0] === '#{key}'", timeout=10_000)
    if view and f"/{view}" not in page.evaluate("location.hash"):
        page.locator(f'#viewTabs a[data-view="{view}"]').tap()
        page.wait_for_function(f"location.hash.indexOf('/{view}') !== -1", timeout=10_000)
    page.wait_for_timeout(400)


# --- 3 ------------------------------------------------------------------------------------------
def test_signed_in_through_hosted_ui_then_app_mode(request, S):
    page = attach(request, S.page)
    assert page.evaluate("document.documentElement.classList.contains('onca-app')"), "not in app mode"
    assert page.evaluate("document.body.classList.contains('is-authed')")
    assert "code=" not in page.url, page.url
    # the session came back through the broker's refresh cookie (the token was removed pre-reload)
    assert page.evaluate("!!sessionStorage.getItem('onca_id_token')")
    host = urlparse(BASE_URL).hostname
    rt = [c for c in S.ctx.cookies() if c["domain"].lstrip(".") == host and c["path"].startswith("/api/session")]
    assert rt and all(c["httpOnly"] for c in rt), "no HttpOnly session-broker cookie"
    print(f"\n[e2e_app] hosted-UI login {S.login_s:.1f}s, client {S.live_auth['client_id'][:6]}…")


# --- 4 ------------------------------------------------------------------------------------------
def test_tab_bar_switches_officer_views(request, S):
    page = attach(request, S.page)
    bar = page.locator("#officerTabs")
    assert bar.is_visible()
    box = bar.bounding_box()
    assert box and abs(box["y"] + box["height"] - 844) <= 2, f"tab bar is not at the bottom: {box}"
    labels = [t.strip() for t in page.locator("#officerTabs .ot-l").all_inner_texts()]
    assert labels == [o[1] for o in OFFICERS], labels
    for el in page.locator("#officerTabs .ot-l").all():
        assert el.is_visible()

    problems = []
    seen = {}
    for key, label in OFFICERS[1:] + OFFICERS[:1]:    # start away from the default (CSO)
        before_hash, before = page.evaluate("location.hash"), page.inner_text("#board")
        _goto_tab(page, key, label)
        tab = page.locator(f'#officerTabs a[data-officer="{key}"]')
        if tab.get_attribute("aria-current") != "page":
            problems.append(f"{label}: tab not marked current")
        h = page.evaluate("location.hash")
        if h == before_hash:
            problems.append(f"{label}: hash did not change ({h})")
        board = page.inner_text("#board")
        if board == before or board in seen.values():
            problems.append(f"{label}: the view did not change")
        seen[key] = board
        shot(page, f"tab-{key}")
    assert not problems, problems


def test_identity_strip(request, S):
    page = attach(request, S.page)
    _goto_tab(page, "cso", "Estratégia")
    who = page.locator("#whoAmI")
    assert who.is_visible()
    text = who.inner_text()
    assert re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), f"no e-mail in the identity strip: {text!r}"
    assert S.persona["username"].lower() in text.lower()
    assert "setores" in text, f"no 'setores' in the identity strip: {text!r}"


def test_account_sheet_without_purchase_cta(request, S):
    page = attach(request, S.page)
    page.locator("#acctBtn").tap()
    _wait_drawer(page)
    assert page.inner_text("#drTitle").strip() == "Sua conta"
    body = page.locator("#drBody")
    plan = page.evaluate("""(() => { const dt = Array.from(document.querySelectorAll('#drBody dt'))
        .find(d => d.textContent.trim() === 'Plano'); return dt ? dt.nextElementSibling.textContent.trim() : null; })()""")
    assert plan and plan != "—", f"no plan in the account sheet ({plan!r})"
    chips = body.locator(".acct-secs .chip")
    assert chips.count() >= 1, "no sector chips in the account sheet"
    assert "Gerencie seu plano em onssa.org." in body.inner_text()
    text = page.inner_text("#drawer")
    assert not PURCHASE.search(text), f"purchase wording in the account sheet: {PURCHASE.search(text).group(0)!r}"
    hrefs = page.eval_on_selector_all("#drawer a[href]", "as => as.map(a => a.href)")
    assert not [h for h in hrefs if "signals-llc.store" in h or "/pricing" in h or "assinar" in h.lower()], hrefs
    shot(page, "account-sheet")
    _close_drawer(page)


def _luminance(rgb: str) -> float:
    r, g, b = (int(x) / 255 for x in re.findall(r"\d+(?:\.\d+)?", rgb)[:3])
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def test_sector_picker_is_dark(request, S):
    page = attach(request, S.page)
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "dark", "app is not pinned dark"
    sel = page.locator("#sectorSel")
    assert sel.is_visible()
    bg, fg = page.evaluate("(() => { const s = getComputedStyle(document.getElementById('sectorSel'));"
                           " return [s.backgroundColor, s.color]; })()")
    assert _luminance(bg) < 0.05, f"sector picker background is not dark: {bg}"
    assert _luminance(fg) > 0.4, f"sector picker text is not light: {fg}"


def test_card_shows_date_and_sources(request, S):
    page = attach(request, S.page)
    problems, opened = [], 0
    for key, label in OFFICERS:
        _goto_tab(page, key, label)
        row = page.locator('#board .exrow[role="button"]').first
        if not row.count():
            continue
        row.scroll_into_view_if_needed()
        row.tap()
        _wait_drawer(page)
        opened += 1
        title, text = page.inner_text("#drTitle"), page.inner_text("#drBody")
        srcs = page.locator("#drBody a[href^='http']").count()
        if not DATE.search(text):
            problems.append(f"{label} / {title!r}: no date")
        if srcs < 1:
            problems.append(f"{label} / {title!r}: no source link")
        if BAD_TEXT.search(title + "\n" + text):
            problems.append(f"{label} / {title!r}: {BAD_TEXT.search(title + text).group(0)!r} in the card")
        if key == "cso":
            shot(page, "card")
        _close_drawer(page)
    assert opened >= 1, "no card could be opened on any tab"
    assert not problems, problems


def test_no_bad_text_and_no_horizontal_scroll(request, S):
    page = attach(request, S.page)
    problems = []
    for key, label in OFFICERS:
        for view in ("hoje", "painel"):
            _goto_tab(page, key, label, view)
            text = page.evaluate("document.body.innerText")
            for m in BAD_TEXT.finditer(text):
                ctx = text[max(0, m.start() - 40): m.end() + 40].replace("\n", " ⏎ ")
                problems.append(f"{label}/{view}: {m.group(0)!r} in …{ctx}…")
            sw = page.evaluate("document.scrollingElement.scrollWidth")
            if sw > 390:
                wide = page.evaluate("""(() => Array.from(document.querySelectorAll('body *')).filter(e => {
                    const r = e.getBoundingClientRect(); return r.right > 391 && r.width > 0 &&
                    getComputedStyle(e).position !== 'fixed'; }).slice(0, 3).map(e => e.tagName.toLowerCase() +
                    (e.id ? '#' + e.id : '') + '.' + String(e.className).split(' ')[0]))()""")
                problems.append(f"{label}/{view}: horizontal scroll, scrollWidth={sw} ({wide})")
            if view == "painel":
                shot(page, f"tab-{key}-painel")
    _goto_tab(page, "cso", "Estratégia")
    assert S.watch.page_errors == [], S.watch.page_errors
    if S.watch.console_errors:
        print(f"\n[e2e_app] console errors while signed in: {S.watch.console_errors[:5]}")
    assert not problems, problems


# --- 6 ------------------------------------------------------------------------------------------
def _feed_cached(page, timeout_s=20):
    js = "caches.open('onca-user-feed').then(c => c.keys()).then(k => k.length)"
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if page.evaluate(js):
            return True
        page.wait_for_timeout(500)
    return False


def test_offline_shows_last_feed(request, S, pw):
    """Offline relaunch: the LAST feed of this user, labelled offline — not a blank page.

    Playwright's WebKit port fails every navigation while offline, even one its active service
    worker could answer ("WebKit encountered an internal error"), so on WebKit this step runs in
    Chromium (iPhone 15 profile, app flag) on the SAME session (cookies + storage carried over).
    """
    if S.engine == "webkit":
        browser = pw.chromium.launch()
        ctx = new_context(pw, browser, storage_state=S.ctx.storage_state())
        page = attach(request, ctx.new_page())
        page.goto(f"{BASE_URL}/exec", wait_until="networkidle")
        page.wait_for_selector("body.is-authed", timeout=45_000)
    else:
        browser, ctx, page = None, S.ctx, attach(request, S.page)
    try:
        page.wait_for_function("!!(navigator.serviceWorker && navigator.serviceWorker.controller)", timeout=30_000)
        assert _feed_cached(page), "the per-user offline feed was never written"
        ctx.set_offline(True)
        page.reload(wait_until="load")
        bar = page.locator("#offlineBar")
        bar.wait_for(state="visible", timeout=30_000)
        assert "offline" in bar.inner_text()
        assert page.evaluate("document.body.classList.contains('is-authed')"), "offline launch lost the session view"
        rows = page.locator("#board .exrow").count()
        assert rows > 0 and len(page.inner_text("#board")) > 200, "offline launch rendered no feed"
        assert "offline" in page.locator("#whoAmI").inner_text()
        shot(page, "offline")
    finally:
        ctx.set_offline(False)
        if browser:
            ctx.close()
            browser.close()


# --- 5 (last: it ends the session) --------------------------------------------------------------
def test_sign_out_through_account_sheet(request, S):
    page = attach(request, S.page)
    _close_drawer(page)
    page.evaluate("window.__shellMsgs = []")
    page.locator("#acctBtn").tap()
    _wait_drawer(page)
    page.get_by_role("button", name="Sair desta conta").tap()
    page.wait_for_function("(window.__shellMsgs || []).some(m => JSON.parse(m).type === 'logout')", timeout=15_000)
    page.wait_for_timeout(800)
    # the page also asks the shell for a push token / lock state (request-reply, typed with an id);
    # the sign-out hand-off is the only auth-class message
    msgs = [m for m in shell_messages(page) if m.get("type") in ("auth", "logout")]
    assert len(msgs) == 1, msgs
    m = msgs[0]
    assert m["type"] == "logout" and set(m) == {"type", "url"}, m
    assert m["url"].startswith(f"{AUTH_ORIGIN}/logout"), m["url"]
    q = _q(m["url"])
    assert q.get("client_id") == S.live_auth["client_id"] and q.get("logout_uri") == "onca://auth", q
    # every trace of the user is gone from the page
    assert not page.evaluate("sessionStorage.getItem('onca_id_token')")
    assert page.evaluate("Object.keys(localStorage).filter(k => k.indexOf('onca_u_') === 0)") == []
    # what the shell does next (App.tsx: go(`${ORIGIN}/exec`)) lands on the gate: session revoked
    page.goto(f"{BASE_URL}/exec", wait_until="networkidle")
    page.wait_for_selector('body[data-gate="auth"]', timeout=30_000)
    assert page.get_by_role("button", name="Entrar").first.is_visible()
    shot(page, "signed-out")
