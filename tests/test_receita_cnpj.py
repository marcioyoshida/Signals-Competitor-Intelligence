import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import receita_cnpj as rc


def test_full_cnpj_reconstructs_matriz_from_raiz():
    # verified live: 57030391 (NDD IP) -> matriz 57030391000104
    assert rc.full_cnpj("57030391") == "57030391000104"
    assert rc.full_cnpj("57.030.391") == "57030391000104"
    assert rc.full_cnpj("57030391000104") == "57030391000104"
    assert rc.full_cnpj("123") is None
    assert rc.full_cnpj(None) is None


def _payload():
    return {
        "razao_social": "NDD INSTITUICAO DE PAGAMENTO LTDA",
        "nome_fantasia": "NDD PAY",
        "cnae_fiscal_descricao": "Atividades auxiliares dos serviços financeiros",
        "data_inicio_atividade": "2024-08-27",
        "capital_social": 1000000,
        "qsa": [
            {"nome_socio": "JACKSON ANTONIO CENCI", "qualificacao_socio": "Administrador"},
            {"nome_socio": "NDD HOLDING FINANCEIRA LTDA", "qualificacao_socio": "Sócio"},
        ],
    }


def test_summarize_extracts_brand_and_prefers_owner():
    s = rc.summarize(_payload())
    assert s["trade_name"] == "NDD PAY"
    assert s["legal_name"].startswith("NDD INSTITUICAO")
    # owner (Sócio) preferred over administrator for the primary controller
    assert s["controller"] == "NDD HOLDING FINANCEIRA LTDA"
    assert "JACKSON ANTONIO CENCI" in s["controllers"]
    assert s["founded"] == "2024-08-27"


def test_enrich_entrants_attaches_and_caps(monkeypatch):
    calls = []

    def fake_fetch(cnpj):
        calls.append(cnpj)
        return _payload()

    entrants = [
        {"id": "bcb-auth:1", "cnpj": "57030391", "name": "NDD IP"},
        {"id": "bcb-auth:2", "cnpj": "57030391", "name": "same raiz"},  # cache hit
        {"id": "bcb-auth:3", "cnpj": "11222333", "name": "other"},
        {"id": "bcb-auth:4", "cnpj": None, "name": "no cnpj"},
    ]
    rc.enrich_entrants(entrants, max_lookups=5, fetcher=fake_fetch, pause_sec=0)

    assert entrants[0]["trade_name"] == "NDD PAY"
    assert entrants[0]["controller"] == "NDD HOLDING FINANCEIRA LTDA"
    assert entrants[1]["trade_name"] == "NDD PAY"          # served from cache
    assert calls.count("57030391") == 1                     # same raiz fetched once
    assert entrants[3].get("trade_name") is None            # no cnpj -> skipped


def test_enrich_entrants_respects_max_lookups(monkeypatch):
    calls = []
    entrants = [{"id": f"e{i}", "cnpj": f"1122233{i}"} for i in range(5)]
    rc.enrich_entrants(entrants, max_lookups=2,
                       fetcher=lambda c: calls.append(c) or {"nome_fantasia": "X"},
                       pause_sec=0)
    assert len(calls) == 2
