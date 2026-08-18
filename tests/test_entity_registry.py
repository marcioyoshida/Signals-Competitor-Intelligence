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

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}


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


def _entrant(**kw):
    base = {
        "id": "bcb-auth:11222333000181",
        "cnpj": "11.222.333/0001-81",
        "name": "ZAPBANK INSTITUICAO DE PAGAMENTO S.A.",
        "trade_name": "ZapBank",
        "legal_name": "ZAPBANK INSTITUICAO DE PAGAMENTO S.A.",
        "controllers": ["ZAP HOLDING LTDA"],
        "license_class": "IP",
        "is_fintech": True,
    }
    base.update(kw)
    return base


def test_auto_create_from_entrant_writes_and_is_idempotent():
    t = FakeTable()
    eid = er.auto_create_from_entrant(_entrant(), table=t)
    assert eid == "zapbank"
    ent = er.get_entity(eid, table=t)
    assert ent["confidence"] == "cnpj" and ent["cnpj_roots"] == ["11222333"]
    assert ent["controllers"] == ["ZAP HOLDING LTDA"] and ent["sector"] == "fintech"
    # resolvable by CNPJ root and by both brand + legal name (future signals)
    assert er.resolve_by_cnpj("11.222.333/0001-81", table=t) == "zapbank"
    assert er.resolve_by_alias("ZapBank", table=t) == "zapbank"
    assert er.resolve_by_alias("ZAPBANK INSTITUICAO DE PAGAMENTO S.A.", table=t) == "zapbank"
    # second pass is a no-op (already mapped by CNPJ)
    assert er.auto_create_from_entrant(_entrant(), table=t) is None


def test_auto_create_skips_without_cnpj():
    t = FakeTable()
    assert er.auto_create_from_entrant(_entrant(cnpj=None), table=t) is None
    assert er.auto_create_from_entrant(_entrant(cnpj="123"), table=t) is None  # too short


def test_auto_create_disambiguates_slug_collision_without_clobbering():
    t = FakeTable()
    # a curated entity already owns the slug for a *different* CNPJ
    er.put_entity("zapbank", "ZapBank (curated)", ["ZAPBANK"],
                  cnpj_roots=["99999999"], confidence="curated", table=t)
    eid = er.auto_create_from_entrant(_entrant(), table=t)
    assert eid == "zapbank_11222333"  # disambiguated, did not overwrite
    assert er.get_entity("zapbank", table=t)["confidence"] == "curated"
    assert er.resolve_by_cnpj("11222333", table=t) == "zapbank_11222333"


def test_load_alias_map_returns_raw_forms():
    t = FakeTable()
    er.put_entity("nubank", "Nubank", ["NUBANK", "NU"],
                  alias_forms=["NUBANK", " NU ", "TICKER:NU"], table=t)
    er.clear_cache()
    m = er.load_alias_map(table=t)
    assert m["nubank"] == ["NUBANK", " NU ", "TICKER:NU"]  # raw curated forms preserved
    er.clear_cache()
