import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import entity_registry as er


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        self.items[Item["pk"]] = Item

    def get_item(self, Key):
        it = self.items.get(Key["pk"])
        return {"Item": it} if it is not None else {}


def test_normalize_alias_folds_accents_and_case():
    assert er.normalize_alias("Itaú  Unibanco") == "ITAU UNIBANCO"
    assert er.normalize_alias("nu pagamentos") == "NU PAGAMENTOS"


def test_put_entity_writes_lookup_items_and_resolves():
    t = FakeTable()
    er.put_entity(
        "nubank", "Nubank / Nu Holdings",
        ["NUBANK", "NU PAGAMENTOS", "TICKER-skip-not-used"],
        cnpj_roots=["18236120"], ticker="NU", sector="pagamentos",
        confidence="curated", table=t,
    )
    ent = er.get_entity("nubank", table=t)
    assert ent["display_name"] == "Nubank / Nu Holdings"
    assert "NUBANK" in ent["aliases"] and ent["confidence"] == "curated"
    assert ent["canonical_id"] == "nubank" and ent["cnpj_roots"] == ["18236120"]
    # lookup items
    assert er.resolve_by_alias("nubank", table=t) == "nubank"
    assert er.resolve_by_alias("Nu Pagamentos", table=t) == "nubank"
    assert er.resolve_by_cnpj("18.236.120/0001-58", table=t) == "nubank"  # root extracted
    assert er.resolve_by_alias("unknown", table=t) is None


def test_seed_from_curated_aliases_populates_registry():
    t = FakeTable()
    n = er.seed(table=t)
    assert n >= 15  # all curated entities
    # a curated entity resolves by alias, and the TICKER: form is indexed by its ticker
    assert er.resolve_by_alias("BTG PACTUAL", table=t) == "btg"
    assert er.resolve_by_alias("STNE", table=t) == "stone"          # from TICKER:STNE
    assert er.resolve_by_alias("Creditas", table=t) == "creditas"
    ent = er.get_entity("infinitepay", table=t)
    assert ent and ent["confidence"] == "curated"
    assert er.normalize_alias("CloudWalk") in ent["aliases"]
