"""ADR 022 Tier-3 — opex/ativo + #146 cost of credit, from the balancete P&L (offline).

The figures are Banco do Brasil's real 202606 balancete lines, so these tests double as the
reconciliation record: net PDD of R$35.76bn against the R$34.40bn BB itself filed with CVM
as `3.04.01` for the same span (ratio 1.04).
"""
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import bcb_resultados as res

_CSV = (
    "Balancete\nData\nFonte\n"
    "#DATA_BASE;DOCUMENTO;CNPJ;AGENCIA;NOME_INSTITUICAO;COD_CONGL;NOME_CONGL;TAXONOMIA;CONTA;NOME_CONTA;SALDO\n"
    "202606;4010;00000000;;BCO DO BRASIL S.A.;;;X;8170000004;(-) Despesas Administrativas;-18550000000,00\n"
    "202606;4010;00000000;;BCO DO BRASIL S.A.;;;X;1000000009;Ativo Realizável;2380000000000,00\n"
    "202606;4010;00000000;;BCO DO BRASIL S.A.;;;X;1600000007;Operações de Crédito;885100000000,00\n"
    "202606;4010;00000000;;BCO DO BRASIL S.A.;;;X;8199200005;(-) DESPESAS DE PROVISÃO;-140850000000,00\n"
    "202606;4010;00000000;;BCO DO BRASIL S.A.;;;X;7199200006;REVERSÃO DE PROVISÃO;105090000000,00\n"
    "202606;4016;00000000;;BCO DO BRASIL S.A.;;;X;8170000004;doc 4016 ignored;-1,00\n"
)


def _patch(monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("202606BANCOS.CSV", _CSV.encode("latin-1"))
    class _R:
        content = buf.getvalue()
        def raise_for_status(self): pass
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _R())


def test_fetch_month_extracts_opex_and_ativo(monkeypatch):
    _patch(monkeypatch)
    d = res.fetch_month(202606)["00000000"]
    assert d["opex"] == 18.55e9 and d["ativo"] == 2380e9   # opex is |value|
    assert d["credito"] == 885.1e9
    assert d["pdd_desp"] == 140.85e9 and d["pdd_rev"] == 105.09e9


def test_map_annualises_ytd_opex_over_ativo(monkeypatch):
    _patch(monkeypatch)
    recs = res.map_to_entities(res.fetch_month(202606), 202606, resolver=lambda i: ["bb"])
    r = recs[0]
    assert r["entity"] == "bb"
    # 18.55e9 * (12/6) / 2380e9 * 100 = 1.56%
    assert r["opex_ativo_pct"] == 1.56


def test_projection_and_merge(monkeypatch):
    _patch(monkeypatch)
    recs = res.map_to_entities(res.fetch_month(202606), 202606, resolver=lambda i: ["bb"])
    idx = res.merge(None, recs)
    assert idx["month"] == 202606 and idx["count"] == 1
    assert res.resultados_by_entity(idx) == {
        "bb": {"opex_ativo_pct": 1.56, "custo_credito_pct": 8.08, "month": 202606}}


# --- #146: cost of credit -------------------------------------------------------------

def test_cost_of_credit_is_net_and_annualised(monkeypatch):
    """(140.85 - 105.09) * (12/6) / 885.1 * 100 = 8.08%. The GROSS despesa alone would read
    31.8% — that 4x is the whole reason #92 was parked as 'came out ~2x high'."""
    _patch(monkeypatch)
    r = res.map_to_entities(res.fetch_month(202606), 202606, resolver=lambda i: ["bb"])[0]
    assert r["custo_credito_pct"] == 8.08
    assert r["credito_bi"] == 885.1


def test_cost_of_credit_is_none_without_the_reversao_leg():
    """A month carrying the expense but not the reversão must yield nothing. The despesa on
    its own is provision TURNOVER — BB reads R$109.75bn of it in January against a PDD stock
    of R$5.62bn — so a partial read is not a conservative estimate, it is a 4-25x error."""
    month = {"00000000": {"name": "BCO DO BRASIL S.A.", "ativo": 2380e9, "opex": 18.55e9,
                          "credito": 885.1e9, "pdd_desp": 140.85e9}}
    r = res.map_to_entities(month, 202606, resolver=lambda i: ["bb"])[0]
    assert r["custo_credito_pct"] is None
    assert r["opex_ativo_pct"] == 1.56          # the other ratio is unaffected


def test_institution_with_only_cost_of_credit_is_kept():
    month = {"11111111": {"name": "BCO X", "ativo": 100e9, "credito": 50e9,
                          "pdd_desp": 3e9, "pdd_rev": 1e9}}
    r = res.map_to_entities(month, 202606, resolver=lambda i: ["x"])[0]
    assert r["opex_ativo_pct"] is None and r["custo_credito_pct"] == 8.0


def test_institution_with_neither_ratio_is_dropped():
    month = {"11111111": {"name": "BCO X", "ativo": 100e9}}
    assert res.map_to_entities(month, 202606, resolver=lambda i: ["x"]) == []


def test_cost_of_credit_is_suppressed_for_a_bank_that_does_not_lend():
    """Custody/clearing banks carry a group-level provision flow against a ~zero loan book.
    Live on 2026-09-19 that produced Citibank N.A. at -30.04% and BOFA Merrill Lynch at
    +46.84%. The gate is on the DENOMINATOR, never on the computed value."""
    month = {"1": {"name": "CITIBANK N.A.", "ativo": 26.3e9, "credito": 0.02e9,
                   "pdd_desp": 2e9, "pdd_rev": 0.1e9, "opex": 0.3e9}}
    r = res.map_to_entities(month, 202606, resolver=lambda i: ["citibank-n"])[0]
    assert r["custo_credito_pct"] is None
    assert r["opex_ativo_pct"] is not None      # the efficiency read is unaffected


def test_a_real_lender_with_an_extreme_ratio_still_surfaces():
    """The gate must not quietly become an outlier filter — a genuinely high cost of credit
    at a real lender is a signal, not an error."""
    month = {"1": {"name": "BCO X", "ativo": 48.8e9, "credito": 20.5e9,
                   "pdd_desp": 3.0e9, "pdd_rev": 0.8e9}}
    r = res.map_to_entities(month, 202606, resolver=lambda i: ["x"])[0]
    assert r["custo_credito_pct"] == 21.46


def test_cost_of_credit_is_suppressed_when_credit_is_not_the_dominant_receivable():
    """A card issuer's receivables sit in OUTROS CRÉDITOS, not Operações de Crédito, so a
    card-sized provision flow over a loan-sized carteira measures the wrong thing. #149's
    CNPJ matching surfaced these for the first time and they arrived reading Afinz 98.08%,
    Digimais 95.30%, Carrefour/CSF 89.27%."""
    month = {"1": {"name": "BCO AFINZ S.A. - BM", "ativo": 2.0e9, "credito": 0.59e9,
                   "outros_creditos": 0.99e9,          # 1.70x the carteira
                   "pdd_desp": 0.4e9, "pdd_rev": 0.1e9, "opex": 0.2e9}}
    r = res.map_to_entities(month, 202606, resolver=lambda i: ["afinz"])[0]
    assert r["custo_credito_pct"] is None
    assert r["opex_ativo_pct"] is not None


def test_a_lender_whose_book_is_mostly_real_credit_is_unaffected():
    """Banco do Brasil's outros/credito is 0.44 — every plausible institution measured on
    2026-09-20 sat below 1.0 and every implausible one above it."""
    month = {"1": {"name": "BCO DO BRASIL S.A.", "ativo": 2380e9, "credito": 885.1e9,
                   "outros_creditos": 385.5e9,
                   "pdd_desp": 140.85e9, "pdd_rev": 105.09e9, "opex": 18.55e9}}
    r = res.map_to_entities(month, 202606, resolver=lambda i: ["bb"])[0]
    assert r["custo_credito_pct"] == 8.08


# --- #151: one row per institution, not per id it was ever called ---------------------

def test_merge_evicts_a_stale_entity_holding_the_same_cnpj():
    """#149 switched this ingester from name-first to CNPJ-first resolution, which moved
    several institutions onto a different entity_id. A plain upsert kept the old id too,
    so one balance sheet was stored twice — 11 CNPJs and 10.8% of total assets on the
    live store. The incoming record must evict the twin."""
    old = res.merge(None, [{"entity": "abc_brasil", "cnpj": "28195667", "ativo_bi": 61.6,
                            "month": 202606, "opex_ativo_pct": 1.0}])
    new = res.merge(old, [{"entity": "abc", "cnpj": "28195667", "ativo_bi": 61.6,
                           "month": 202606, "opex_ativo_pct": 1.0}])
    assert set(new["records"]) == {"abc"}
    assert new["count"] == 1

    # an unrelated institution is untouched, and re-running is stable
    two = res.merge(new, [{"entity": "bb", "cnpj": "00000000", "ativo_bi": 2380.0,
                           "month": 202606, "opex_ativo_pct": 1.56}])
    assert set(two["records"]) == {"abc", "bb"}
    assert set(res.merge(two, list(two["records"].values()))["records"]) == {"abc", "bb"}
