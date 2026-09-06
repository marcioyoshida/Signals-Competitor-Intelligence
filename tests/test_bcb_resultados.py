"""ADR 022 Tier-3 — operating efficiency (opex/ativo) from the balancete P&L (offline)."""
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
    "202606;4010;00000000;;BCO DO BRASIL S.A.;;;X;1600000007;Operações de Crédito (ignored);885000000000,00\n"
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
    assert "1600000007" not in d                            # non-target account ignored


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
    assert res.resultados_by_entity(idx) == {"bb": {"opex_ativo_pct": 1.56, "month": 202606}}
