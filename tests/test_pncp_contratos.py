"""PNCP federal-contracts ingester (#62)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import pncp_contratos as pc


def _page(rows, total_pages=1):
    return {"data": rows, "totalPaginas": total_pages}


_ROWS = [
    {"numeroControlePncpCompra": "111-1-000047/2026", "tipoPessoa": "PJ",
     "niFornecedor": "11111111000111", "nomeRazaoSocialFornecedor": "ACME LTDA",
     "valorGlobal": 250000.0, "objetoContrato": "Serviços de TI",
     "dataAssinatura": "2026-08-20", "dataVigenciaInicio": "2026-08-25",
     "dataVigenciaFim": "2027-08-25", "orgaoEntidade": {"razaoSocial": "MIN X", "cnpj": "999"}},
    # pessoa física — skipped
    {"numeroControlePncpCompra": "111-1-000048/2026", "tipoPessoa": "PF",
     "niFornecedor": "12345678901", "valorGlobal": 5000.0},
    # small PJ contract — kept unless min_valor filters it
    {"numeroControlePncpCompra": "111-1-000049/2026", "tipoPessoa": "PJ",
     "niFornecedor": "22222222000122", "nomeRazaoSocialFornecedor": "OUTRA SA",
     "valorGlobal": 1200.0, "objetoContrato": "Café", "dataAssinatura": "2026-08-21",
     "orgaoEntidade": {"razaoSocial": "MIN Y"}},
]


def test_fetch_parses_pj_only_and_normalizes():
    rows = pc.fetch_contracts(fetcher=lambda d1, d2, p, s: _page(_ROWS))
    assert len(rows) == 2                              # the PF row is dropped
    acme = next(r for r in rows if r["cnpj_root"] == "11111111")
    assert acme["id"] == "pncp:111-1-000047/2026" and acme["valor"] == 250000.0
    assert acme["buyer"] == "MIN X" and acme["company"] == "ACME LTDA"


def test_fetch_min_valor_filters_small_contracts():
    rows = pc.fetch_contracts(fetcher=lambda d1, d2, p, s: _page(_ROWS), min_valor=100000)
    assert {r["cnpj_root"] for r in rows} == {"11111111"}   # the R$1.2k café dropped


def test_fetch_paginates_until_total_pages_and_cap():
    calls = {"n": 0}

    def fetcher(d1, d2, page, size):
        calls["n"] += 1
        return _page(_ROWS, total_pages=3)             # 3 pages available

    rows = pc.fetch_contracts(fetcher=fetcher, max_pages=2)  # cap below total
    assert calls["n"] == 2                             # stopped at the cap, not total
    assert len(rows) == 4                              # 2 PJ rows × 2 pages


def test_map_is_cnpj_only_and_stamps_entities():
    rows = pc.fetch_contracts(fetcher=lambda d1, d2, p, s: _page(_ROWS))
    recs = pc.map_to_entities(rows, cnpj_index={"11111111": "acme"})
    assert len(recs) == 1                              # only the tracked CNPJ resolves
    rec = recs[0]
    assert rec["entity"] == "acme" and rec["_entities"] == ["acme"]
    assert rec["id"] == "contracts:acme:pncp:111-1-000047/2026"
    assert "250,000" in rec["title"] and "ACME" in rec["text"].upper()
    assert pc.map_to_entities(rows, cnpj_index={}) == []     # no fuzzy name fallback


def test_summarize_aggregates_valor():
    rows = pc.fetch_contracts(fetcher=lambda d1, d2, p, s: _page(_ROWS))
    recs = pc.map_to_entities(rows, cnpj_index={"11111111": "acme", "22222222": "outra"})
    s = pc.summarize(recs)
    assert s["total"] == 2 and s["entities"] == 2
    top = {t["entity"]: t["total_valor"] for t in s["top"]}
    assert top["acme"] == 250000.0 and top["outra"] == 1200.0


def test_store_merge_idempotent_by_id():
    rows = pc.fetch_contracts(fetcher=lambda d1, d2, p, s: _page(_ROWS))
    recs = pc.map_to_entities(rows, cnpj_index={"11111111": "acme"})
    merged = pc.merge({"records": {recs[0]["id"]: recs[0]}}, recs)
    assert merged["count"] == 1 and pc.list_records(merged)[0]["entity"] == "acme"
