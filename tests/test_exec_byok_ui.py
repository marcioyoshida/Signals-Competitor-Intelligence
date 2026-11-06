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
    i_read, i_clear, i_send = JS.index("const apiKey = key.value.trim()"), JS.index('key.value = ""'), JS.index('modelAct("connect_model"')
    assert i_read < i_clear < i_send
    for leak in ("localStorage", "sessionStorage", "console.", "apiKey}"):
        assert leak not in JS, leak
    key_input = JS[JS.index('<input type="password" id="bkKey"'):]
    assert "value=" not in key_input[:key_input.index(">")]            # never pre-filled
    assert not re.search(r"bkKey[^;]*\.value\s*=\s*[^\"]", JS)          # nothing but "" is ever written into it


def test_connect_needs_the_key_and_the_acknowledgement():
    assert "go.disabled = !(key.value.trim() && ack.checked)" in JS
    assert "acknowledged: ack.checked" in JS and "S.acknowledgement" in JS


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
    for field in ("c.label || c.model", "c.last4", "S.acknowledgement", "m.label", "st.j.error"):
        assert f"U.esc({field}" in JS, field
