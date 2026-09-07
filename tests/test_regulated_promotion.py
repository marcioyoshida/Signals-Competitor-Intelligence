"""#103: regulated-registry entrant promotion — classify + SPA/SUSEP roster backfill."""
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import entity_discovery as ed
from src.synth import entity_registry as er


class _FakeTable:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def get_item(self, Key):
        it = self.items.get(Key["pk"])
        return {"Item": it} if it else {}

    def put_item(self, Item):
        self.items[Item["pk"]] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key["pk"], None)

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}


# ── classify_industries (the delta path) ─────────────────────────────────────────
def test_classify_regulated_license_classes():
    assert er.classify_industries({"license_class": "Casa de apostas (SPA/MF)"}) == (["betting"], False)
    assert er.classify_industries(
        {"license_class": "Seguradora (vida/previdência)"}) == (["insurance"], False)
    assert er.classify_industries({"license_class": "Resseguradora"}) == (["insurance"], False)
    assert er.classify_industries(
        {"license_class": "EFPC (previdência complementar fechada)"}) == (["closed-pension"], False)


def test_classify_explicit_industry_and_fallthrough():
    assert er.classify_industries({"industry": "asset-management"}) == (["asset-management"], False)
    assert er.classify_industries({"industry": "not-real"}) == ([], True)   # invalid → review
    assert er.classify_industries({"license_class": "Mystery"}) == ([], True)
    # fintech still wins when nothing else matches
    assert er.classify_industries({"is_fintech": True}) == (["fintech"], False)


# ── profile builders ─────────────────────────────────────────────────────────────
def test_profile_from_susep_cleans_brand_and_gates_generic():
    p = ed._profile_from_susep({"name": "PORTO SEGURO CIA DE SEGUROS GERAIS", "cnpj": "61198164000160"})
    assert p["display_name"] == "Porto Seguro" and p["entity_id"] == "porto-seguro"
    assert p["auto_ok"] is True and p["news_search"] is False
    # bare-generic (leading brand word stripped as an org word) → route to review
    g = ed._profile_from_susep({"name": "CAIXA SEGURADORA S.A.", "cnpj": "34020354000110"})
    assert g["auto_ok"] is False


def test_profile_from_spa_prefers_brand_and_aliases_domains():
    p = ed._profile_from_spa(
        {"name": "NSX ENTERTAINMENT LTDA", "cnpj": "11222333000144",
         "brands": ["Betano"], "domains": ["betano.bet.br"]})
    assert p["display_name"] == "Betano" and p["entity_id"] == "betano"
    assert "NSX ENTERTAINMENT LTDA" in p["aliases"] and "betano.bet.br" in p["aliases"]
    assert p["news_search"] is True


# ── promote_roster ───────────────────────────────────────────────────────────────
def test_promote_creates_standalone_insurer_structured():
    table = _FakeTable()
    rows = [{"name": "SUL AMERICA COMPANHIA NACIONAL DE SEGUROS", "cnpj": "29978814000187"}]
    rep = ed.promote_roster(rows, industry="insurance",
                            profile_fn=ed._profile_from_susep, table=table)
    assert rep["created"] == ["sul-america"]
    ent = table.items["ENT#sul-america"]
    assert ent["industries"] == ["insurance"] and ent["confidence"] == "structured"
    assert "29978814" in ent["cnpj_roots"]


def test_promote_enriches_existing_by_cnpj():
    table = _FakeTable()
    er.put_entity("mapfre", "Mapfre", ["MAPFRE"], cnpj_roots=["61074175"],
                  industries=["insurance"], confidence="curated", table=table)
    rows = [{"name": "MAPFRE SEGUROS GERAIS S.A.", "cnpj": "61074175000193"}]
    rep = ed.promote_roster(rows, industry="insurance",
                            profile_fn=ed._profile_from_susep, table=table)
    # resolves by CNPJ → no create/duplicate; the legal name is added as a match alias
    assert rep["created"] == [] and rep["enriched"] == ["mapfre"]


def test_promote_budget_exhausted_is_skipped_not_proposed():
    table = _FakeTable()
    rows = [{"name": f"SEGURADORA MARCA{i} S.A.", "cnpj": f"{30000000 + i:08d}000191"}
            for i in range(5)]
    rep = ed.promote_roster(rows, industry="insurance", profile_fn=ed._profile_from_susep,
                            max_new=2, table=table)
    assert len(rep["created"]) == 2 and len(rep["skipped"]) == 3 and rep["proposed"] == []


def test_promote_same_id_collision_is_proposed_not_overwritten():
    """A 'Porto Seguro' insurer CNPJ must not clobber the curated porto_seguro entity."""
    table = _FakeTable()
    er.put_entity("porto_seguro", "Porto Seguro", ["PORTO SEGURO"], cnpj_roots=["61198164"],
                  industries=["insurance"], confidence="curated", table=table)
    # a DIFFERENT CNPJ whose brand also slugs to porto_seguro
    rows = [{"name": "PORTO SEGURO VIDA CIA DE SEGUROS", "cnpj": "99999999000191"}]
    rep = ed.promote_roster(rows, industry="insurance",
                            profile_fn=ed._profile_from_susep, table=table)
    assert rep["created"] == [] and rep["proposed"]           # proposed, not created
    assert table.items["ENT#porto_seguro"]["confidence"] == "curated"  # untouched


def test_promote_no_brand_is_proposed_for_review():
    table = _FakeTable()
    rows = [{"name": "CAIXA SEGURADORA S.A.", "cnpj": "34020354000110"}]
    rep = ed.promote_roster(rows, industry="insurance",
                            profile_fn=ed._profile_from_susep, table=table)
    assert rep["created"] == [] and rep["proposed"]  # no clean brand → curator names it


def test_promote_spa_creates_betting_entity_with_brand_alias():
    table = _FakeTable()
    rows = [{"name": "NSX ENTERTAINMENT LTDA", "cnpj": "11222333000144",
             "brands": ["Betano"], "domains": ["betano.bet.br"]}]
    rep = ed.promote_roster(rows, industry="betting",
                            profile_fn=ed._profile_from_spa, table=table)
    assert rep["created"] == ["betano"]
    ent = table.items["ENT#betano"]
    assert ent["industries"] == ["betting"]
    assert er.resolve_by_alias("Betano", table=table) == "betano"


def test_auto_create_from_entrant_honors_confidence():
    table = _FakeTable()
    eid = er.auto_create_from_entrant(
        {"cnpj": "12345678000199", "name": "ACME SEGUROS S.A.",
         "license_class": "Seguradora"}, confidence="structured", table=table)
    assert eid and table.items[f"ENT#{eid}"]["confidence"] == "structured"
    assert table.items[f"ENT#{eid}"]["industries"] == ["insurance"]  # regulated class classified
