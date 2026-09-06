"""ADR 022 Tier B — monthly COSIF balancete trajectory (offline, fixture-based)."""
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import bcb_balancete as bal

_CSV = (
    "Balancete\nData\nFonte\n"
    "#DATA_BASE;DOCUMENTO;CNPJ;AGENCIA;NOME_INSTITUICAO;COD_CONGL;NOME_CONGL;TAXONOMIA;CONTA;NOME_CONTA;SALDO\n"
    "202606;4010;00000000;;BCO DO BRASIL S.A.;;;X;1600000007;OPERAÇÕES DE CRÉDITO;885144512970,00\n"
    "202606;4010;00000000;;BCO DO BRASIL S.A.;;;X;4100000009;DEPÓSITOS;946578045790,00\n"
    "202606;4010;00000000;;BCO DO BRASIL S.A.;;;X;6000000004;Patrimônio Líquido;175415862980,00\n"
    "202606;4010;00000000;;BCO DO BRASIL S.A.;;;X;1899900004;(-) PROVISÃO CRÉDITO;-6652348940,00\n"
    "202606;4010;00000000;;BCO DO BRASIL S.A.;;;X;7110000003;Rendas (ignored);999,00\n"
    "202606;4016;00000000;;BCO DO BRASIL S.A.;;;X;1600000007;doc 4016 ignored;1,00\n"
)


def _zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("202606BANCOS.CSV", _CSV.encode("latin-1"))
    return buf.getvalue()


def test_num_parses_decimal_comma():
    assert bal._num("2259788502441,17") == 2259788502441.17
    assert bal._num("-6652348940,00") == -6652348940.0
    assert bal._num("") is None and bal._num("x") is None


def test_fetch_month_maps_codes_to_lines_and_ignores_others(monkeypatch):
    class _R:
        content = _zip_bytes()
        def raise_for_status(self): pass
    monkeypatch.setattr(bal.requests, "get", lambda *a, **k: _R())
    data = bal.fetch_month(202606)
    bb = data["00000000"]
    assert bb["name"] == "BCO DO BRASIL S.A."
    # R$ → R$ mil (÷1000)
    assert bb["lines"]["credito"] == 885144512.97
    assert bb["lines"]["depositos"] == 946578045.79
    assert bb["lines"]["patrimonio_liquido"] == 175415862.98
    assert bb["lines"]["pdd"] == 6652348.94         # sign flipped to positive magnitude
    assert "7110000003" not in bb["lines"]           # non-target account ignored


def test_map_to_entities_largest_credito_wins():
    data = {"A": {"name": "ITAU FIN", "lines": {"credito": 10.0}},
            "B": {"name": "ITAU", "lines": {"credito": 50.0}}}
    per = bal.map_to_entities(data, resolver=lambda i: ["itau"])
    assert set(per) == {"itau"} and per["itau"]["lines"]["credito"] == 50.0


def test_append_month_is_append_only_and_sorted():
    per = {"bb": {"name": "BB", "lines": {"credito": 100.0, "pdd": 5.0}}}
    idx = bal.append_month(None, 202605, per)
    idx = bal.append_month(idx, 202606, {"bb": {"name": "BB", "lines": {"credito": 110.0, "pdd": 6.0}}})
    idx = bal.append_month(idx, 202606, {"bb": {"name": "BB", "lines": {"credito": 999.0}}})  # dup month → no-op
    s = idx["records"]["bb"]["series"]
    assert [p["month"] for p in s] == [202605, 202606]
    assert s[-1]["credito"] == 110.0 and idx["latest_month"] == 202606
    assert idx["line_map"]["credito"] == "1600000007"


def test_trajectory_computes_mom_pct():
    idx = bal.append_month(None, 202605, {"bb": {"name": "BB", "lines": {"credito": 100.0, "pdd": 5.0}}})
    idx = bal.append_month(idx, 202606, {"bb": {"name": "BB", "lines": {"credito": 110.0, "pdd": 6.0}}})
    t = bal.trajectory(idx["records"]["bb"])
    assert t["month"] == 202606 and t["credito"] == 110.0 and t["credito_mom_pct"] == 10.0
    assert t["pdd_mom_pct"] == 20.0
