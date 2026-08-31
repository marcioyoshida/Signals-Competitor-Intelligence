"""CEIS/CNEP sanctions ingester (#60)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import ceis_cnep as cc

# A minimal ;-delimited, quoted CSV in the real column layout (pessoa J + pessoa F).
_HEADER = ('"CADASTRO";"CÓDIGO DA SANÇÃO";"TIPO DE PESSOA";"CPF OU CNPJ DO SANCIONADO";'
           '"NOME DO SANCIONADO";"RAZÃO SOCIAL - CADASTRO RECEITA";"CATEGORIA DA SANÇÃO";'
           '"DATA INÍCIO SANÇÃO";"DATA FINAL SANÇÃO";"ÓRGÃO SANCIONADOR";"FUNDAMENTAÇÃO LEGAL"')
_CEIS_CSV = "\n".join([
    _HEADER,
    '"CEIS";"9001";"J";"11.111.111/0001-11";"ACME PAGAMENTOS SA";"ACME PAGAMENTOS S.A.";'
    '"Impedimento";"03/04/2026";"03/04/2028";"TCU";"LEI 8.666"',
    # pessoa física — dropped (no CNPJ)
    '"CEIS";"9002";"F";"24442755604";"FULANO";"";"Impedimento";"01/01/2026";"";"TJMG";"LEI 8429"',
    # a J row for a company we do NOT track — kept in fetch, dropped in map
    '"CEIS";"9003";"J";"99.999.999/0001-99";"OUTRA LTDA";"OUTRA LTDA";"Suspensão";'
    '"05/05/2026";"05/05/2027";"CGU";"LEI 12.846"',
])


def _dl(kind):
    return ("2026-08-28", _CEIS_CSV) if kind == "ceis" else None


def test_cnpj_root_and_iso_date():
    assert cc._cnpj_root("11.111.111/0001-11") == "11111111"
    assert cc._cnpj_root("24442755604") is None          # CPF, too short
    assert cc._iso_date("03/04/2026") == "2026-04-03"
    assert cc._iso_date("2026-04-03") == "2026-04-03"
    assert cc._iso_date("") is None


def test_fetch_parses_juridica_only():
    rows = cc.fetch_sanctions(kinds=("ceis", "cnep"), downloader=_dl)
    assert len(rows) == 2                                 # the pessoa física row is skipped
    r = next(r for r in rows if r["cnpj_root"] == "11111111")
    assert r["id"] == "ceis:9001" and r["cadastro"] == "CEIS"
    assert r["company"] == "ACME PAGAMENTOS S.A." and r["start"] == "2026-04-03"


def test_map_resolves_by_cnpj_root_and_stamps_entities():
    rows = cc.fetch_sanctions(downloader=_dl)
    recs = cc.map_to_entities(rows, cnpj_index={"11111111": "acme"})
    assert len(recs) == 1                                 # only the tracked CNPJ resolves
    rec = recs[0]
    assert rec["entity"] == "acme" and rec["_entities"] == ["acme"]
    assert rec["id"] == "sanctions:acme:ceis:9001"
    assert "ACME" in rec["text"].upper() and rec["title"].startswith("CEIS")


def test_map_is_cnpj_only_no_name_matching():
    # By design there is NO name fallback: a sanctioned CNPJ absent from the registry is
    # dropped, never fuzzy-matched to a same-token entity (defamation/LGPD guard).
    rows = cc.fetch_sanctions(downloader=_dl)
    assert cc.map_to_entities(rows, cnpj_index={}) == []
    assert cc.map_to_entities(rows, cnpj_index={"99999999": "outra"})[0]["entity"] == "outra"


def test_build_cnpj_index():
    idx = cc.build_cnpj_index([
        {"entity_id": "acme", "cnpj_roots": ["11111111", "22222222"]},
        {"entity_id": "nope"},
    ])
    assert idx["11111111"] == "acme" and idx["22222222"] == "acme"


def test_summarize_and_store_merge():
    rows = cc.fetch_sanctions(downloader=_dl)
    recs = cc.map_to_entities(rows, cnpj_index={"11111111": "acme"})
    s = cc.summarize(recs)
    assert s["total"] == 1 and s["entities"] == 1 and s["by_cadastro"]["CEIS"] == 1
    merged = cc.merge({"records": {"sanctions:acme:ceis:9001": recs[0]}}, recs)
    assert merged["count"] == 1                           # idempotent by id
    assert cc.list_records(merged)[0]["entity"] == "acme"
