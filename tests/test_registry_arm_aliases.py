"""Registry fixes from #309's follow-up (2026-11-02).

- A sub-entity's bare brand alias belongs to its parent: ALIAS#SAFRA resolved to Safra Corretora,
  and SANTANDER/NEON/MERCADO PAGO to their brokerages (39 keys live).
- A DASFN institution that no name resolves falls back to its CNPJ root: "STONE PAGAMENTOS S.A."
  never resolved because STONE is an ambiguous token, though root 16501555 is `stone`'s."""
from src.ingest import bcb_km1, pilar3_dasfn
from src.synth import entity_registry as er
from tests.test_entity_registry import FakeTable


def _ents():
    return [
        {"entity_id": "safra", "display_name": "Safra", "aliases": ["BANCO SAFRA S.A.", "SAFRA"]},
        {"entity_id": "safra-corretora", "display_name": "Safra Corretora", "parent": "safra",
         "aliases": ["SAFRA", "SAFRA DISTRIBUIDORA DE TITULOS E VALORES MOBILIARIOS LTDA."]},
        # a JV keeps its own name even when the parent lists it
        {"entity_id": "zurich_seguros", "display_name": "Zurich", "aliases": ["ZURICH SANTANDER"]},
        {"entity_id": "zurich-santander", "display_name": "Zurich Santander", "parent": "zurich_seguros",
         "aliases": ["ZURICH SANTANDER"]},
        # a long legal name the parent wrongly carries is the arm's identity, not the parent's brand
        {"entity_id": "sparta", "display_name": "Sparta", "aliases": ["SPARTA", "SPARTA FIAGRO FUNDO"]},
        {"entity_id": "craa11", "display_name": "SPARTA CRAA11", "parent": "sparta",
         "aliases": ["SPARTA FIAGRO FUNDO"]},
        # a self-parent loop is left alone
        {"entity_id": "mercantil", "display_name": "Mercantil", "parent": "mercantil", "aliases": ["MERCANTIL"]},
    ]


def test_only_the_parents_own_brand_moves():
    owner = {"SAFRA": "safra-corretora", "ZURICH SANTANDER": "zurich-santander",
             "SPARTA FIAGRO FUNDO": "craa11", "MERCANTIL": "mercantil", "BANCO SAFRA S.A.": "safra"}
    assert er.find_arm_held_aliases(_ents(), owner) == [
        {"key": "SAFRA", "holder": "safra-corretora", "parent": "safra"}]


def test_repoint_is_a_dry_run_unless_asked():
    t = FakeTable()
    for e in _ents():
        t.put_item(Item={"pk": f"ENT#{e['entity_id']}", "type": "entity", **e})
    t.put_item(Item={"pk": "ALIAS#SAFRA", "type": "alias", "entity_id": "safra-corretora"})
    assert len(er.repoint_arm_aliases(t)) == 1
    assert er.resolve_by_alias("Safra", table=t) == "safra-corretora"          # dry run wrote nothing
    er.repoint_arm_aliases(t, dry_run=False)
    assert er.resolve_by_alias("Safra", table=t) == "safra"
    assert er.repoint_arm_aliases(t) == []                                     # idempotent


def test_a_sub_entity_put_never_takes_the_parents_key():
    t = FakeTable()
    er.put_entity("safra", "Safra", ["BANCO SAFRA S.A.", "SAFRA"], source="curated", table=t)
    er.put_entity("safra-corretora", "Safra Corretora", ["SAFRA", "SAFRA DTVM"], parent="safra",
                  source="curated", table=t)
    assert er.resolve_by_alias("SAFRA", table=t) == "safra"
    assert er.resolve_by_alias("SAFRA DTVM", table=t) == "safra-corretora"     # its own name still indexes
    assert "SAFRA" in er.get_entity("safra-corretora", table=t)["aliases"]


def test_cnpj_is_a_fallback_after_the_name():
    by_cnpj = {"16501555000157": "stone", "58160789000128": "safra-twin"}.get
    nothing = lambda item: []
    assert bcb_km1.resolve_institution("16501555000157", "STONE PAGAMENTOS S.A.", nothing,
                                       by_cnpj=by_cnpj) == ["stone"]
    # a name that resolves wins, so CNPJ never moves a record onto a discovered twin (#149)
    assert bcb_km1.resolve_institution("58160789000128", "BCO SAFRA S.A.", lambda i: ["safra"],
                                       by_cnpj=by_cnpj) == ["safra"]
    assert bcb_km1.resolve_institution(None, "X", nothing, by_cnpj=by_cnpj) == []
    assert bcb_km1.resolve_institution("99999999000100", "X", nothing, by_cnpj=by_cnpj) == []


def test_banks_missing_from_the_stored_index_are_fetched_first(monkeypatch):
    seen = []
    monkeypatch.setattr(pilar3_dasfn, "bank_prose", lambda tables, **k: seen.append(tables["ova"]) or ("", []))
    res = {c: {"name": n, "year": 2025, "tables": {"ova": n}}
           for c, n in [("1", "AAA BANCO"), ("2", "BBB BANCO"), ("3", "ZZZ BANCO")]}
    pilar3_dasfn.build(res, resolver=lambda i: [i["title"][:3].lower()], industries_of=lambda e: None,
                       known={"1", "2"}, sleep=lambda s: None)
    assert seen == ["ZZZ BANCO", "AAA BANCO", "BBB BANCO"]


def test_a_zero_km1_ratio_is_not_applicable():
    """Stone reports LCR 0 (outside the LCR rule): it must not become a 0% "crítica" alarm."""
    doc = {"km1_7": {"t": 0.1566}, "km1_17": {"t": 0, "t_1": 0, "t_2": 0, "t_3": 0, "t_4": 0}}
    km = bcb_km1.extract_km1(doc)
    assert km["basileia_pct"] == 15.66 and km["lcr_pct"] is None and bcb_km1.lcr_band(km["lcr_pct"]) is None
    assert km["lcr_series_pct"] == [None] * 5 and "lcr_qoq_pp" not in km
