import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import cvm_financials as cf  # noqa: E402
from src.synth import valuation  # noqa: E402


def _row(code, label, val, scale="MIL"):
    return {"CD_CONTA": code, "DS_CONTA": label, "VL_CONTA": str(val), "ESCALA_MOEDA": scale}


# --- #92 cost-to-income ---------------------------------------------------------------

def _bb_layout():
    # BB H1-2026, as filed (R$ mil): provision BELOW 3.03, pessoal at 3.04.03.
    return [
        _row("3.01", "Receitas de Intermediação Financeira", 160747114),
        _row("3.02", "Despesas de Intermediação Financeira", -111283805),
        _row("3.03", "Resultado Bruto de Intermediação Financeira", 49463309),
        _row("3.04.01", "Despesa de Provisão para Perda Esperada para Risco de Crédito", -34397820),
        _row("3.04.02", "Receitas de Prestação de Serviços", 17934696),
        _row("3.04.03", "Despesas com Pessoal", -14280659),
        _row("3.04.04", "Outras Despesas de Administrativas", -4580140),
    ]


def test_cost_to_income_matches_hand_computed_bb():
    out = cf._bank_efficiency(_bb_layout())
    assert out["cost_to_income"] == round(18860799 / 67398005, 4)  # 0.2798


def test_cost_to_income_adds_back_provision_booked_inside_3_02():
    rows = [
        _row("3.01", "Receitas da Intermediação Financeira", 200),
        _row("3.02.01", "Despesas com Juros e Similares", -80),
        _row("3.02.02", "(Perda) de Crédito Esperada com Operações de Crédito", -20),
        _row("3.03", "Resultado Bruto Intermediação Financeira", 100),
        _row("3.04.01", "Receitas de Prestação de Serviços", 0),
        _row("3.04.02", "Despesas de Pessoal", -30),
        _row("3.04.03", "Outras Despesas Administrativas", -6),
    ]
    # pre-provision margin = 100 + 20 = 120 → 36 / 120
    assert cf._bank_efficiency(rows)["cost_to_income"] == 0.3


def test_cost_to_income_is_none_for_non_bank_layout():
    rows = [_row("3.01", "Receita de Venda de Bens e/ou Serviços", 100),
            _row("3.03", "Resultado Bruto", 40), _row("3.04.01", "Despesas com Vendas", -5)]
    assert cf._bank_efficiency(rows) is None


def test_cost_to_income_is_none_when_a_line_is_missing():
    rows = [r for r in _bb_layout() if "Pessoal" not in r["DS_CONTA"]]
    assert cf._bank_efficiency(rows) is None


# --- #93 shares ------------------------------------------------------------------------

def test_parse_shares_nets_treasury_and_keeps_newest():
    rows = [
        {"CNPJ_CIA": "00.000.000/0001-91", "DT_REFER": "2026-03-31",
         "QT_ACAO_ORDIN_CAP_INTEGR": "10", "QT_ACAO_PREF_CAP_INTEGR": "0",
         "QT_ACAO_ORDIN_TESOURO": "1", "QT_ACAO_PREF_TESOURO": "0"},
        {"CNPJ_CIA": "00.000.000/0001-91", "DT_REFER": "2026-06-30",
         "QT_ACAO_ORDIN_CAP_INTEGR": "5730834040", "QT_ACAO_PREF_CAP_INTEGR": "0",
         "QT_ACAO_ORDIN_TESOURO": "21862562", "QT_ACAO_PREF_TESOURO": "0"},
    ]
    out = cf._parse_shares(rows)["00000000"]
    assert out == {"shares_on": 5708971478.0, "shares_pn": 0.0, "shares_as_of": "2026-06-30"}


# --- #93 valuation ----------------------------------------------------------------------

_PRICES = {"BBAS3": 22.17, "ITUB3": 46.29, "ITUB4": 42.37, "SANB3": 14.79, "SANB4": 14.61}


def _quote(sym):
    p = _PRICES.get(sym)
    return {"price": p, "date": "2026-09-23"} if p else None


def test_single_class_issuer_valued_at_raw_scale():
    rec = {"shares_on": 5708971478.0, "shares_pn": 0, "equity": 190767860000.0,
           "net_income": 20e9, "months": 12}
    v = valuation.value(rec, "BBAS3", _quote)
    assert v["share_scale"] == 1
    assert round(v["market_cap"] / 1e9) == 127
    assert v["pe"] == round(v["market_cap"] / 20e9, 1)


def test_thousands_filer_is_rescaled_by_price_to_book():
    # Itaú files 5,617,743 ON / 5,404,129 PN — thousands, with no unit column.
    rec = {"shares_on": 5617743.0, "shares_pn": 5404129.0, "equity": 228026000000.0}
    v = valuation.value(rec, "ITUB4", _quote)
    assert v["share_scale"] == 1000
    assert 480e9 < v["market_cap"] < 500e9


def test_unit_ticker_prices_each_class_not_the_unit():
    rec = {"shares_on": 3813193.0, "shares_pn": 3674334.0, "equity": 129093065000.0}
    v = valuation.value(rec, "SANB11", _quote)
    expected = (3813193 * 14.79 + 3674334 * 14.61) * 1000
    assert v["market_cap"] == round(expected, 2)


def test_withheld_when_a_class_has_no_price():
    rec = {"shares_on": 10.0, "shares_pn": 10.0, "equity": 100.0}
    assert valuation.value(rec, "BBAS3", _quote) is None  # BBAS4/5/6 do not quote


def test_withheld_when_neither_scale_is_plausible():
    rec = {"shares_on": 1.0, "shares_pn": 0, "equity": 1e15}
    assert valuation.value(rec, "BBAS3", _quote) is None


def test_ticker_root_with_a_digit_is_accepted():
    rec = {"shares_on": 4998298548.0, "shares_pn": 0, "equity": 18729402000.0}
    v = valuation.value(rec, "B3SA3", lambda s: {"price": 18.04, "date": "2026-09-23"} if s == "B3SA3" else None)
    assert round(v["market_cap"] / 1e9) == 90


def test_immaterial_golden_share_class_does_not_withhold():
    # IRB: 81,168,796 ON + exactly 1 PN; IRBR4 does not quote.
    rec = {"shares_on": 81168796.0, "shares_pn": 1.0, "equity": 5328366000.0}
    v = valuation.value(rec, "IRBR3", lambda s: {"price": 59.46, "date": "2026-09-23"} if s == "IRBR3" else None)
    assert round(v["market_cap"] / 1e9, 1) == 4.8


def test_bdr_is_skipped():
    rec = {"shares_on": 1e9, "shares_pn": 0, "equity": 1e10}
    assert valuation.value(rec, "ROXO34", _quote) is None


def test_pe_withheld_on_interim_only_profit():
    rec = {"shares_on": 5708971478.0, "shares_pn": 0, "equity": 190767860000.0,
           "net_income": 10e9, "months": 6}
    assert valuation.value(rec, "BBAS3", _quote)["pe"] is None


def test_interim_shares_preferred_over_annual():
    rec = {"shares_on": 1.0, "shares_pn": 0, "equity": 190767860000.0,
           "interim": {"shares_on": 5708971478.0, "shares_pn": 0, "shares_as_of": "2026-06-30",
                       "equity": 190767860000.0}}
    v = valuation.value(rec, "BBAS3", _quote)
    assert v["shares_as_of"] == "2026-06-30" and v["share_scale"] == 1
