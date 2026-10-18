"""Layer 1, signed out (#259): the /exec gate as the store app shows it, and the sign-in hand-off
to the shell (ADR 029: the provider in the SYSTEM browser, back on onca://auth). Since #277 "Entrar"
opens Onça's own form in the WebView (no Cognito page); only Google/Apple hand off to the shell."""
from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

from tests.e2e_app.harness import AUTH_ORIGIN, BASE_URL, shell_messages, shot

# Rule #258 (App Store 3.1.1): no purchase call to action and no price inside the app.
PURCHASE_WORDS = ("Assinar", "R$")

# Elements that start inside the top 44px (the status-bar band on a notched iPhone) must be whole:
# not above the viewport, not past its sides, and not painted over by something else.
TOP_BAND_CUT = r"""
(band) => {
  const out = [];
  const vw = document.documentElement.clientWidth;
  const visible = (el) => {
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== "none" && s.visibility !== "hidden" && +s.opacity > 0 && r.width > 0 && r.height > 0;
  };
  // anything that paints text of its own, plus controls and icons
  const ownText = (el) => Array.from(el.childNodes).some((n) => n.nodeType === 3 && n.textContent.trim());
  const leaves = Array.from(document.body.querySelectorAll("*")).filter((el) =>
    visible(el) && (ownText(el) || el.matches("button, a, input, select, svg")));
  for (const el of leaves) {
    const r = el.getBoundingClientRect();
    if (r.top >= band || r.bottom <= 0) continue;                   // not in the band
    const name = el.tagName.toLowerCase() + (el.id ? "#" + el.id : "") +
      (el.className && typeof el.className === "string" ? "." + el.className.split(" ")[0] : "") +
      " '" + (el.textContent || "").trim().slice(0, 20) + "'";
    if (r.top < -0.5) { out.push(name + ` top=${r.top.toFixed(1)}`); continue; }
    if (r.left < -0.5 || r.right > vw + 0.5) { out.push(name + ` x=${r.left.toFixed(1)}..${r.right.toFixed(1)}`); continue; }
    const cx = Math.min(Math.max(r.left + r.width / 2, 0), vw - 1), cy = Math.max(r.top + r.height / 2, 0);
    if (getComputedStyle(el).pointerEvents === "none") continue;     // hit-testing can't see it
    const hit = document.elementFromPoint(cx, cy);
    // its own centre must hit it (or a child): an ancestor or a sibling there means it is painted over
    if (!hit || (hit !== el && !el.contains(hit))) out.push(name + " covered by " + (hit ? hit.tagName.toLowerCase() : "nothing"));
  }
  return out;
}
"""


def _open_gate(page):
    page.goto(f"{BASE_URL}/exec", wait_until="networkidle")
    page.wait_for_selector('body[data-gate="auth"]', timeout=30_000)
    page.get_by_role("button", name="Entrar").first.wait_for(state="visible")
    page.wait_for_timeout(500)   # late console errors / layout settle


def test_signed_out_gate(app_page):
    page = app_page
    _open_gate(page)
    assert page.evaluate("document.documentElement.classList.contains('onca-app')"), \
        "context.js did not take app mode from window.__ONCA_APP__"

    # "Entrar" is the one visible action
    entrar = page.get_by_role("button", name="Entrar")
    assert entrar.first.is_visible()

    # #258: no "Assinar", no price — in the rendered text, the title, or any hidden markup
    text = page.evaluate("document.body.innerText")
    markup = page.evaluate("""(() => { const c = document.documentElement.cloneNode(true);
        c.querySelectorAll('script,style,template').forEach(e => e.remove()); return c.textContent; })()""")
    for w in PURCHASE_WORDS:
        assert w not in text, f"{w!r} visible on the app gate"
        assert w not in markup, f"{w!r} present (hidden) in the app gate markup"
    hrefs = page.eval_on_selector_all("a[href]", "as => as.map(a => a.href)")
    assert not [h for h in hrefs if "signals-llc.store" in h or "/pricing" in h], hrefs

    # nothing went wrong while booting
    assert page.watch.page_errors == [], page.watch.page_errors
    assert page.watch.console_errors == [], page.watch.console_errors

    # nothing under the top 44px is cut (the old fixed-inset bug hid the top of /exec)
    assert page.evaluate("window.scrollY") == 0
    cut = page.evaluate(TOP_BAND_CUT, 44)
    assert cut == [], f"cut or covered in the top 44px: {cut}"
    brand = page.locator(".appbar .brand").bounding_box()
    assert brand and brand["y"] >= 0 and brand["y"] + brand["height"] <= 844, brand
    box = entrar.first.bounding_box()
    assert box and box["y"] >= 44 and box["y"] + box["height"] <= 844, f"Entrar not fully on screen: {box}"
    shot(page, "gate")


def _b64url_sha256(s: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(s.encode()).digest()).decode().rstrip("=")


def test_entrar_opens_the_form_and_google_posts_one_auth_message(app_page, live_auth):
    page = app_page
    _open_gate(page)
    page.get_by_role("button", name="Entrar").first.tap()
    page.wait_for_selector("#__signin .si__card", timeout=10_000)
    page.wait_for_timeout(500)
    assert shell_messages(page) == [], "Entrar must open the in-page form, not hand off to the shell"
    # #277: every control on the sheet is a real touch target, text inputs don't trigger the iOS zoom
    small = page.evaluate("""() => Array.from(document.querySelectorAll('#__signin input, #__signin button, #__signin a'))
        .filter((e) => e.offsetParent).map((e) => [e.id || e.className, e.getBoundingClientRect().height,
          e.tagName === 'INPUT' ? parseFloat(getComputedStyle(e).fontSize) : 99]).filter((r) => r[1] < 44 || r[2] < 16)""")
    assert small == [], f"controls under 44px / 16px: {small}"
    page.locator('#__signin [data-idp="Google"]').tap()
    page.wait_for_function("(window.__shellMsgs || []).length > 0", timeout=10_000)
    page.wait_for_timeout(1000)  # a second (duplicate) message would land here

    msgs = shell_messages(page)
    assert len(msgs) == 1, f"expected exactly one shell message, got {msgs}"
    m = msgs[0]
    assert set(m) == {"type", "url"} and m["type"] == "auth", m
    url = m["url"]
    assert url.startswith(f"{AUTH_ORIGIN}/oauth2/authorize?"), url
    assert url.startswith(live_auth["domain"] + "/"), (url, live_auth)
    assert "identity_provider=Google" in url, url

    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["redirect_uri"] == "onca://auth", q
    assert q["code_challenge_method"] == "S256", q
    assert q["response_type"] == "code", q
    assert q["client_id"] == live_auth["client_id"], "page and live context.js disagree on the client id"

    # the verifier waits in sessionStorage (the shell reloads /exec?code=… in this same WebView)
    verifier = page.evaluate("sessionStorage.getItem('onca_pkce_verifier')")
    assert verifier and 43 <= len(verifier) <= 128, "no PKCE verifier in sessionStorage"
    assert q["code_challenge"] == _b64url_sha256(verifier), "code_challenge is not S256(verifier)"

    # the page did NOT navigate itself: the shell opens the system browser
    assert page.url.startswith(f"{BASE_URL}/exec"), page.url
