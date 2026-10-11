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
