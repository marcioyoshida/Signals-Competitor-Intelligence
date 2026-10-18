"""#263 — pt-BR number formatting for server-built display strings."""
import math
from decimal import Decimal
from pathlib import Path

import pytest

from src.synth.fmt_br import compact, num, pct, pp, signed, signed_pct
from src.synth.text_pt import pt_count


@pytest.mark.parametrize("x, nd, out", [
    (19.4, 1, "19,4"), (19.44, 1, "19,4"), (1234.5, 1, "1.234,5"), (1234567.891, 2, "1.234.567,89"),
    (0.0, 1, "0,0"), (-3.21, 1, "-3,2"), (-0.04, 1, "0,0"), (15.0, 1, "15,0"), (0.4, 2, "0,40"),
    (42, 1, "42"), (1234, 1, "1.234"), (-1234, 0, "-1.234"), (999, 0, "999"),
    (Decimal("12.5"), 1, "12,5"), (Decimal("24"), 1, "24"),
])
def test_num(x, nd, out):
    assert num(x, nd) == out


@pytest.mark.parametrize("x", [None, math.nan, math.inf, -math.inf, "19.4", True, Decimal("NaN")])
def test_num_missing_is_a_dash(x):
    assert num(x) == "—" and pct(x) == "—" and signed(x) == "—" and pp(x) == "—"


def test_pct_signed_pp_compact():
    assert pct(19.4) == "19,4%" and pct(118.0) == "118,0%" and pct(67, 0) == "67%"
    assert signed(25.0) == "+25,0" and signed(-3.2) == "-3,2" and signed(0.0) == "0,0"
    assert signed(0.45, 2) == "+0,45" and signed(1234.5) == "+1.234,5" and signed(-0.01) == "0,0"
    assert signed_pct(6.9) == "+6,9%" and signed_pct(-55.56) == "-55,6%"
    assert pp(2.3) == "2,3 p.p."
    assert compact(3.0) == "3" and compact(3.25) == "3,2" and compact(24.0) == "24" and compact(1.5) == "1,5"


def test_minus_matches_the_client_formatter():
    # /exec's nf() is toLocaleString("pt-BR"), which writes the ASCII hyphen-minus.
    assert num(-1.2) == "-1,2" and "−" not in num(-1.2)


def test_pt_count_groups_thousands():
    assert pt_count(1, "fonte", "fontes") == "1 fonte"
    assert pt_count(1234, "fonte", "fontes") == "1.234 fontes"


def test_client_nf_passes_server_strings_through():
    """/exec formats numbers itself; server strings must never be re-formatted (no double format)."""
    page = (Path(__file__).resolve().parents[1] / "src" / "dashboard" / "site" / "v3" / "index.html").read_text(encoding="utf-8")
    assert 'if (typeof v !== "number" || !isFinite(v)) return v;' in page
    # the server-written display strings are escaped as text, never passed to nf()
    for field in ("r.text", "t.briefing", "m.ets_note", "m.tdr_note", "w.headline", "r.detail", "r.title"):
        assert f"nf({field}" not in page, field
