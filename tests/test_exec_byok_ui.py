"""#348 BYOK: the account sheet's "Modelo próprio" drawer handles the key as write-only."""
import re
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "src" / "dashboard" / "site" / "v3" / "index.html").read_text(encoding="utf-8")
JS = PAGE[PAGE.index("async function openOwnModel()"):PAGE.index("// #251 self-service deletion")]


def test_entry_point_is_on_the_account_sheet_for_tenant_members_only():
    assert 'id="acctModel">Modelo próprio (IA)</button>' in PAGE
    assert '(canWatch() ? `<button class="btn" type="button" id="acctModel">' in PAGE   # not the operator preview


def test_the_key_field_is_write_only():
    assert '<input type="password" id="bkKey" autocomplete="off"' in JS and 'spellcheck="false"' in JS
    assert 'autocomplete="off">' in JS                                   # the form too
    # read once, cleared BEFORE the request goes out; never stored, never echoed back
    i_read, i_clear, i_send = JS.index("args.api_key = f.value.trim()"), JS.index('f.value = ""'), JS.index('modelAct("connect_model"')
    assert i_read < i_clear < i_send
    for leak in ("localStorage", "sessionStorage", "console.", "api_key}"):
        assert leak not in JS, leak
    key_input = JS[JS.index('<input type="password" id="bkKey"'):]
    assert "value=" not in key_input[:key_input.index(">")]            # never pre-filled
    assert not re.search(r"bkKey[^;]*\.value\s*=\s*[^\"]", JS)          # nothing but "" is ever written into it


def test_connect_needs_the_key_and_the_acknowledgement():
    assert "go.disabled = !(f && f.value.trim() && ack.checked)" in JS
    assert "acknowledged: ack.checked" in JS and "S.acknowledgement" in JS
    # #350: switching the model re-asks for the (provider-specific) acknowledgement
    assert "ack.checked = false" in JS and "S.acknowledgements" in JS


def test_status_shows_last4_and_the_errors_meaning_only():
    assert "•••• ${U.esc(c.last4)}" in JS and "c.last_error_message" in JS
    assert "Usando o modelo da Onça" in JS


def test_manage_actions_follow_the_server_flag_and_remove_needs_two_taps():
    assert "const c = S.connection, manage = !!S.can_manage" in JS
    for intent in ('"model_status"', '"connect_model"', '"test_model"', '"disconnect_model"'):
        assert intent in JS, intent
    assert not re.search(r"(?<![\w.])confirm\(", JS)                   # no JS dialog in the app's WebView
    assert 'del.dataset.armed !== "1"' in JS


def test_every_string_from_the_server_is_escaped():
    for field in ("c.label || c.model", "c.last4", "ackOf(chosen)", "m.label", "st.j.error",
                  "c.role_arn", "c.region", "B.principal", "B.external_id", "B.template_url", "r"):
        assert f"U.esc({field}" in JS, field


def test_bedrock_role_form_takes_an_arn_and_a_region_never_a_secret():
    # #350: the role path sends role_arn + region; the external id comes from the server's own
    # tenant row and is never sent back by the page.
    assert 'id="bkRole"' in JS and 'id="bkRegion"' in JS
    assert "args.role_arn = f.value.trim(); args.region" in JS
    assert "external_id:" not in JS and "args.external_id" not in JS
    role_input = JS[JS.index('<input type="text" id="bkRole"'):]
    assert "value=" not in role_input[:role_input.index(">")]
    assert "roteamento global" in JS                              # sa-east-1 data-residency caveat
