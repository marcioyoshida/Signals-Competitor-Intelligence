"""#242–#245: /v2 sign-in/out via the registered /exec callback; /exec view state in the hash;
dark-theme contrast on the ask button; 44px touch targets."""
from pathlib import Path

SITE = Path(__file__).resolve().parents[1] / "src" / "dashboard" / "site"
CTX = (SITE / "v2" / "context.js").read_text(encoding="utf-8")
EXEC = (SITE / "v3" / "index.html").read_text(encoding="utf-8")
CSS = (SITE / "v2" / "app.css").read_text(encoding="utf-8")
APP = (Path(__file__).resolve().parents[1] / "infra" / "app.py").read_text(encoding="utf-8")


def test_v2_uses_only_registered_cognito_urls():
    # #242: Cognito validates redirect_uri / logout_uri literally — only "/" and "/exec" exist
    assert '["/", "/exec"].indexOf(location.pathname) !== -1 ? location.pathname : "/exec"' in CTX
    for u in ('"https://onssa.org/exec"', '"https://onssa.org/"'):
        assert u in APP
    # the return trip is a same-site path only (no open redirect) and expires
    assert r"/^\/(?!\/)[A-Za-z0-9_\-\/.]*$/" in CTX and "15 * 60 * 1000" in CTX
    assert 'rememberReturnPath("login")' in CTX and 'rememberReturnPath("logout")' in CTX
    # a code-less landing on /exec honours only a LOGOUT trip (an abandoned sign-in never bounces)
    assert 'returnToOrigin("", "logout")' in CTX and 'returnToOrigin(back, "login")' in CTX
    assert "m.k === kind" in CTX


def test_exec_view_state_lives_in_the_hash():
    # #243: officer / view / sector all go through the hash; legacy single tokens still parse
    assert 'writeHash({ officer: a.getAttribute("data-officer") })' in EXEC
    assert 'writeHash({ view: a.getAttribute("data-view") })' in EXEC
    assert "writeHash({ sector: e.target.value })" in EXEC
    assert 'window.addEventListener("hashchange", applyHash);' in EXEC
    assert "OFFICER = hashOfficer() || OFFICER;" in EXEC


def test_ask_button_uses_on_primary_and_touch_targets_are_44px():
    assert ".askbtn { background:var(--primary); color:var(--on-primary);" in EXEC  # #244
    coarse = CSS.split("@media (pointer: coarse)", 1)[1]                              # #245
    for sel in (".drawer__close { min-width: 44px; min-height: 44px;", ".btn { min-height: 44px; }",
                ".view-tabs a, .ctxnav a { min-height: 44px;"):
        assert sel in coarse, sel


def test_entry_tier_resolves_share_links():
    # #246: the Entry branch of boot() returned before resolveSharedLink ran
    entry = EXEC.split("if (!ADMIN && Ctx.isEntryTier(DATA)) {   // ADR 016", 1)[1].split("return;", 1)[0]
    assert "mountEntry();" in entry and "setTimeout(resolveSharedLink, 0)" in entry


def test_store_app_bridge_hands_sign_in_to_the_shell():
    # ADR 029 (#254): only when the shell injected __ONCA_APP__ AND the RN bridge exists
    assert "(global.__ONCA_APP__ && global.ReactNativeWebView) ? global.__ONCA_APP__ : null" in CTX
    assert 'if (APP && APP.redirectUri === "onca://auth") AUTH.redirectUri = APP.redirectUri;' in CTX
    assert 'toShell({ type: "auth", url:' in CTX and 'toShell({ type: "logout", url:' in CTX
    assert APP.count('"onca://auth",') == 2   # Cognito callback + logout URLs


def test_app_mode_shows_no_purchase_call_to_action():
    # #258 (App Store 3.1.1/3.1.3): every purchase CTA has an app-mode branch with no link/price
    assert 'const PLAN_NOTE = "Gerencie seu plano em onssa.org.";' in CTX
    assert "${APP ? `<span class=\"hd-note\">${PLAN_NOTE}</span>`" in CTX                 # trial bar
    assert "${APP ? PLAN_NOTE : `Assine para voltar a ver ${esc(ctxLabel)}.`}" in CTX      # lapsed gate
    assert "const ctas = APP ? (mods.some((m) => bandOf(m))" in CTX                       # entry board
    assert "${band && !APP ? `<a class=\"btn btn--primary\"" in CTX                       # segment gate
    assert 'if (Ctx.inApp()) return "";   // #258' in EXEC                                 # ?assinar
    assert "if (Ctx.inApp()) { Ctx.shareViaShell(url); return; }" in EXEC                  # share sheet
    ne = (SITE / "v2" / "newentry" / "index.html").read_text(encoding="utf-8")
    assert "const links = C.inApp()" in ne
