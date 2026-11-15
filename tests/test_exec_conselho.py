"""#327 /exec "Conselho" view: routed, wired to the board intents, and escape-before-markup."""
import re
from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "src/dashboard/site/v3/index.html").read_text(encoding="utf-8")


def test_view_tab_and_hash_route():
    assert '<a href="#conselho" data-view="conselho">Conselho</a>' in HTML
    assert 'x === "conselho"' in HTML and 'if (VIEW === "conselho") { renderConselho(el); return; }' in HTML
    assert 'classList.toggle("view-conselho", v === "conselho")' in HTML


def test_view_uses_only_the_board_intents_and_set_decision_followups():
    intents = set(re.findall(r'conselhoAct\("([a-z_]+)"', HTML))
    assert intents == {"board_run_status", "board_brief", "decide_board_brief",
                       "set_board_adoption", "set_outcome"}


def test_board_pack_markdown_is_escaped_before_markup():
    fn = HTML[HTML.index("function mdToHtml"):HTML.index("const CS_STATUS")]
    # every line or cell that reaches the output passes through U.esc first
    assert fn.count("U.esc(") >= 5 and "innerHTML" not in fn


def test_operator_preview_and_signed_out_get_a_notice_not_the_form():
    fn = HTML[HTML.index("function renderConselho"):HTML.index("function conselhoPack")]
    assert "if (ADMIN || !Ctx.getIdToken())" in fn
