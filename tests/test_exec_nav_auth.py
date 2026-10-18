"""#242–#245: /v2 sign-in/out via the registered /exec callback; /exec view state in the hash;
dark-theme contrast on the ask button; 44px touch targets."""
from pathlib import Path

SITE = Path(__file__).resolve().parents[1] / "src" / "dashboard" / "site"
CTX = (SITE / "v2" / "context.js").read_text(encoding="utf-8")
EXEC = (SITE / "v3" / "index.html").read_text(encoding="utf-8")
CSS = (SITE / "v2" / "app.css").read_text(encoding="utf-8")
# #262: the Cognito pool/clients/IdPs live in infra/identity.py (built once per pool by app.py),
# so the identity assertions below read both files.
APP = "\n".join((Path(__file__).resolve().parents[1] / "infra" / f).read_text(encoding="utf-8")
                for f in ("app.py", "identity.py"))


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


def test_native_push_and_app_lock_bridge():
    # #256: the page asks the shell for an Expo token and subscribes it like a web endpoint
    assert "function askShell(type, data, timeoutMs)" in CTX and "global.__oncaShell = { reply(id, value)" in CTX
    assert 'const r = await Ctx.askShell("push:token", { request: !!request });' in EXEC
    assert 'const endpoint = "expo:" + r.token;' in EXEC
    assert "if (Ctx.inApp()) return nativeSub(false);" in EXEC
    # #257: lock settings shown only in the app, changed through the shell
    assert 'if (Ctx.inApp()) html += hojeSec("hoje-bloqueio"' in EXEC
    assert 'Ctx.askShell("lock:set", { enabled, minutes:' in EXEC


def test_apple_idp_is_context_gated_and_on_both_clients():
    # #248: like Google — only with all three IDs in infra/cdk.json context; key only in Secrets Manager
    assert 'self.node.try_get_context(f"apple_{k}")' in APP and "if all(_apple.values()):" in APP
    assert '"signalscompetitor/onca/apple-oauth", json_field="private_key"' in APP
    assert "cognito.ProviderAttribute.APPLE_EMAIL_VERIFIED" in APP
    assert APP.count("cognito.UserPoolClientIdentityProvider.COGNITO, *_federated") == 2
    assert "for _idp in (google_idp, apple_idp):" in APP


def test_federated_gate_warns_about_hidden_apple_email():
    assert r"/@privaterelay\.appleid\.com$/i" in CTX and "Compartilhar meu e-mail" in CTX
    assert 'const who = /apple/i.test(idp) ? "Apple" : "Google";' in CTX
    assert "conta individual nova" in CTX
    assert 'quer acompanhar no plano Entry${APP ? "" : " (R$ 490/mês' in CTX   # no price in the app


def _tile_calls():
    """Yield the top-level argument list of every `tile(...)` call in the board code."""
    i = 0
    while True:
        i = EXEC.find("tile(", i)
        if i < 0:
            return
        if EXEC[max(0, i - 8):i].endswith("function "):
            i += 5
            continue
        j, depth, args, cur, q = i + 5, 1, [], "", None
        while depth:
            ch = EXEC[j]
            if q:
                cur += ch
                if ch == "\\":
                    cur += EXEC[j + 1]; j += 1
                elif ch == q:
                    q = None
            elif ch in "\"'`":
                q = ch; cur += ch
            elif ch in "([{":
                depth += 1; cur += ch
            elif ch in ")]}":
                depth -= 1
                if depth:
                    cur += ch
            elif ch == "," and depth == 1:
                args.append(cur.strip()); cur = ""
            else:
                cur += ch
            j += 1
        args.append(cur.strip())
        yield args
        i = j


def test_exec_kpi_tiles_lead_to_their_panel():
    import re
    nav = set(re.findall(r'nav: "([a-z]+)"', EXEC)) | set(re.findall(r'data-nav="([a-z]+)"', EXEC))
    dests = set()
    for args in _tile_calls():
        if len(args) >= 5:
            dests |= set(re.findall(r'"([a-z]+)"', args[4]))
    assert len(dests) >= 14
    # a tile may only point at a panel/band that exists on the board (data-nav) — never a vague spot
    assert dests <= nav, dests - nav
    # activation: real <a>, deep link through the hash, no fake affordance on a 0 / unmapped tile
    assert "function activateTiles(root)" in EXEC and 'a.setAttribute("href", navHash(key))' in EXEC
    assert 'if (!t || empty) { d.removeAttribute("data-go"); return; }' in EXEC
    assert 'if (ev && ev.type === "hashchange" && navOnlyChange()) return;' in EXEC
    assert "a.tile--nav:focus-visible" in EXEC


def test_exec_key_panels_emphasis_is_desktop_only():
    css = EXEC.split(".lead-tag { display:none; }", 1)[1].split("@media (min-width: 861px) {", 1)[1].split("\n  }\n", 1)[0]
    assert ".panel--key > .panel__hd" in css and "var(--primary-weak)" in css and ".lead-tag" in css
    assert "grid-row" not in css                      # masonry unset-span trap: no row spans
    assert EXEC.count("lead: true") >= 8              # CSO, CRO, CCO, CPO lead bands (band + panels)


# --- nav-test findings #278 / #281 / #283 / #284 ------------------------------------------------

def test_exec_touch_targets_are_44px():
    """#278: phone Hoje/Painel segment, evidência links, [n] source markers, chat chips."""
    seg = EXEC.split("#viewTabs a { min-height:", 1)[1]
    assert seg.startswith("44px")                                  # was 38px
    touch = EXEC.split("@media (pointer: coarse) {", 1)[1].split("\n  }\n", 1)[0]
    for needle in ("a[data-ev]::before", ".exrow .meta a::before", ".exrow .ttl-more::before",
                   ".cites .cite", ".askex .chip", "min-height:44px"):
        assert needle in touch, needle
    assert "inset:-16px" in touch                                  # hit-slop, not a bigger visual
    css = CSS.split("@media (pointer: coarse) {", 1)[1]
    assert ".sup a::before" in css and "a.refnum::before" in css   # shared [n] markers (cards, drawer)


def test_public_pages_touch_targets_are_44px():
    """#281: pricing / sample / docs — every link reaches 44px under a coarse pointer."""
    for rel in ("pricing.html", "sample/index.html", "docs/index.html", "docs/privacy.html",
                "docs/terms.html", "docs/celular-seguranca.html"):
        html = (SITE / rel).read_text(encoding="utf-8")
        assert "@media (pointer: coarse)" in html, rel
        blk = html.split("@media (pointer: coarse)", 1)[1].split("</style>", 1)[0]
        assert "a:where(:not(.btn):not(.pbtn):not(.mlink))::after" in blk and "inset: -15px" in blk, rel
    pricing = (SITE / "pricing.html").read_text(encoding="utf-8")
    assert ".mlink { display: inline-flex; align-items: center; min-height: 44px; }" in pricing
    assert ".pbtn, .cta a.btn" in pricing.split("@media (pointer: coarse)", 1)[1]
    sample = (SITE / "sample/index.html").read_text(encoding="utf-8")
    assert ".card .foot a.cite" in sample and ".card p a.cite::after { inset: -16px -17px; }" in sample


def test_exec_painel_row_click_opens_drawer_and_ver_mais_is_explicit():
    """#283: the headline no longer swallows the click; expand is a keyboard-reachable button."""
    assert "t.addEventListener(\"click\"" not in EXEC.split("function wireHeadlines", 1)[1].split("\n}\n", 1)[0]
    wire = EXEC.split("function wireHeadlines(el) {", 1)[1].split("\n}\n", 1)[0]
    assert 'document.createElement("button")' in wire and 'b.type = "button"' in wire
    assert "e.stopPropagation()" in wire                           # only on the button, never the headline
    assert '"ver menos"' in wire and 'setAttribute("aria-expanded"' in wire
    assert "b.hidden = true" in wire                               # hidden until proven clamped
    assert "t.scrollHeight > t.clientHeight + 1" in EXEC           # shown only when actually clamped
    assert "if (narrowScreen() && t.closest(\".exrow[tabindex]\")) return;" not in EXEC
    assert "  wireHeadlines(el);\n  bindRows(el);" in EXEC
    # Enter/Space on the row still opens the drawer, and ignores keys bubbling from the button
    assert "if (e.target !== row || (e.key !== \"Enter\" && e.key !== \" \")) return;" in EXEC
    assert "cursor:pointer; }\n  .exrow .ttl:hover" not in EXEC     # headline no longer styled as its own control


def test_exec_chat_scrolls_to_start_of_new_answer():
    """#284: a finished answer is read from its first line; pending/error follow the end."""
    fn = EXEC.split("function renderChat() {", 1)[1].split("const n = turns.filter", 1)[0]
    assert "ansEl.getBoundingClientRect().top - log.getBoundingClientRect().top" in fn
    assert "!last.pending && !last.error" in fn and ": log.scrollHeight;" in fn
    # the panel must be visible when renderChat measures it
    op = EXEC.split("function openChat(q) {", 1)[1].split("\n}\n", 1)[0]
    assert op.index("chat.hidden = false") < op.index("renderChat()")
