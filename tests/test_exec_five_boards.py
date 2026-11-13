"""ADR 032: five boards with plain names — the page, the phone tab bar and the mobile QA flow
must agree on the board count and never show a C-suite acronym."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "src/dashboard/site/v3/index.html").read_text(encoding="utf-8")


def _officer_keys():
    block = PAGE[PAGE.index("const OFFICERS = {"):PAGE.index("};", PAGE.index("const OFFICERS = {"))]
    return re.findall(r"^\s{2}(\w+): \{ slug:", block, re.M)


def test_five_boards_in_display_order():
    assert _officer_keys() == ["cso", "risk", "cco", "cpo", "cro"]


def test_phone_tab_bar_has_one_column_per_board():
    m = re.search(r"#officerTabs \{[^}]*grid-template-columns:repeat\((\d+),", PAGE)
    assert m and int(m.group(1)) == len(_officer_keys())


def test_no_acronym_in_officer_labels():
    block = PAGE[PAGE.index("const OFFICERS = {"):PAGE.index("};", PAGE.index("const OFFICERS = {"))]
    assert not re.search(r"\b(CSO|CRO|CCO|CPO)\b", block)


def test_mobile_qa_flow_waits_on_plain_board_headings():
    flow = (ROOT / "mobile/apps/onca/maestro/tabbar.yaml").read_text(encoding="utf-8")
    assert not re.search(r"\((CSO|CRO|CCO|CPO)\)", flow)
    for name in ("Estratégia", "Risco", "Compliance", "Produto", "Regulatório"):
        assert f'tapOn: "{name}"' in flow
