"""#287: /exec sends one event per tracked interaction, ids and enums only.

Static checks over the shipped page: every kind/action pair the server accepts has a sender in
/exec (or is a kept legacy pair), every upgrade link is tagged, and nothing text-shaped is ever
passed as an engagement key."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.synth import engagement_log as el  # noqa: E402

SITE = ROOT / "src" / "dashboard" / "site"
EXEC = (SITE / "v3" / "index.html").read_text(encoding="utf-8")
CTX = (SITE / "v2" / "context.js").read_text(encoding="utf-8")
CALLS = re.findall(r'engage\("([a-z]+)",\s*("([a-z]+)"|how)', EXEC)
LEGACY = {("push", "open")}   # older clients sent the opt-in as "open"; still accepted, no sender


def test_every_accepted_pair_has_a_sender():
    sent = {(k, a) for k, _, a in CALLS if a}
    for k, a, b in re.findall(r'engage\("([a-z]+)",\s*\w+ \? "([a-z]+)" : "([a-z]+)"', EXEC):   # headline toggle
        sent |= {(k, a), (k, b)}
    if any(x == "how" for _, x, _ in CALLS):   # signin: the method enum from OncaCtx
        sent |= {("signin", m) for m in el.KINDS["signin"]}
    accepted = {(k, a) for k, acts in el.KINDS.items() for a in acts}
    assert accepted - sent == LEGACY
    assert sent <= accepted, sent - accepted


def test_no_text_or_url_is_ever_a_key():
    for m in re.finditer(r"engage\(([^;]*?)\);", EXEC):
        args = m.group(1)
        assert not re.search(r"textContent|\.value\b|\.href|getAttribute\(\"href\"\)|\bu\)|\.q\b|answer", args), args


def test_every_upgrade_link_is_tagged():
    for src, name in ((CTX, "context.js"), (EXEC, "v3")):
        for line in src.splitlines():
            if "upgradeHref(" in line and "function upgradeHref" not in line:
                assert "data-upg=" in line, (name, line.strip()[:100])


def test_sign_in_method_is_an_enum_read_once():
    assert "takeSignInMethod," in CTX and 'sessionStorage.removeItem(SIGNIN_METHOD_KEY)' in CTX
    assert 'm === "password" || m === "google" || m === "apple" ? m : null' in CTX
    assert 'noteSignInMethod("password");' in CTX and 'noteSignInMethod(useIdp === "Google" ? "google" : "apple");' in CTX
    assert 'Ctx.isLoggedIn() && Ctx.takeSignInMethod()' in EXEC


def test_double_taps_are_sent_once():
    assert "now - (ENGAGED.get(sig) || 0) < 3000" in EXEC


def test_privacy_policy_names_what_is_collected():
    p = (SITE / "docs" / "privacy.html").read_text(encoding="utf-8")
    line = next(l for l in p.splitlines() if "<b>Engajamento.</b>" in l)
    for w in ("fontes", "chat", "relatos", "alertas", "assinatura", "método de entrada", "nunca o texto"):
        assert w in line, w


def test_entry_board_is_counted_too():
    """The Entry branch returns before the officer board: it must log the session itself, and the
    shared drawer (OncaCtx.openCard) announces each card it opens."""
    entry = EXEC[EXEC.index("if (!ADMIN && Ctx.isEntryTier(DATA)) {   // ADR 016"):][:200]
    assert "logSession()" in entry
    assert 'new CustomEvent("onca-card", { detail: { id: c.id || null } })' in CTX
    assert 'addEventListener("onca-card"' in EXEC and 'engage("drawer", "open", CTX_CARD)' in EXEC


def test_a_plan_button_is_an_upgrade_not_a_source():
    """The Entry SaaS button links to the storefront (https) from inside #board: data-upg wins."""
    body = EXEC[EXEC.index("function onOutLink(e) {"):]
    body = body[:body.index("\n}\n")]
    assert body.index('a.hasAttribute("data-upg")') < body.index('a.closest("#drBody")') < body.index('a.closest("#board")')
