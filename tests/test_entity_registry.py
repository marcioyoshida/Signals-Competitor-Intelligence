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


def test_accumulate_aliases_adds_data_derived_form_and_indexes_it():
    t = FakeTable()
    # step 3 auto-created a fintech from its CNPJ; only the brand is known so far
    er.put_entity("zapbank_11222333", "ZapBank", ["ZAPBANK"],
                  cnpj_roots=["11222333"], confidence="cnpj", table=t)
    # a later CVM offering names it by razão social — accumulate that alias
    added = er.accumulate_aliases(
        "zapbank_11222333", ["ZAPBANK INSTITUICAO DE PAGAMENTO S.A."], table=t
    )
    assert added == ["ZAPBANK INSTITUICAO DE PAGAMENTO S.A."]
    # now a name-only signal (news/DOU) using the legal name resolves too
    assert er.resolve_by_alias("Zapbank Instituicao de Pagamento S.A.", table=t) == "zapbank_11222333"
    ent = er.get_entity("zapbank_11222333", table=t)
    assert "ZAPBANK INSTITUICAO DE PAGAMENTO S.A." in ent["alias_forms"]


def test_accumulate_aliases_is_idempotent_and_skips_short_forms():
    t = FakeTable()
    er.put_entity("inter", "Banco Inter", ["INTER"], confidence="curated", table=t)
    assert er.accumulate_aliases("inter", ["Banco Inter S.A."], table=t) == ["Banco Inter S.A."]
    # second pass with the same form writes nothing
    assert er.accumulate_aliases("inter", ["Banco Inter S.A."], table=t) == []
    # too-short forms are rejected (unsafe substring keys)
    assert er.accumulate_aliases("inter", ["IB"], table=t) == []


def test_accumulate_aliases_never_hijacks_another_entitys_name():
    t = FakeTable()
    er.put_entity("stoneco", "StoneCo", ["STONECO", "STONE PAGAMENTOS S.A."],
                  confidence="curated", table=t)
    er.put_entity("stonex", "StoneX", ["STONEX"], confidence="curated", table=t)
    # trying to fold StoneCo's exact name onto StoneX must NOT steal the index
    added = er.accumulate_aliases("stonex", ["STONE PAGAMENTOS S.A."], table=t)
    assert added == ["STONE PAGAMENTOS S.A."]           # raw form kept on stonex
    assert er.resolve_by_alias("STONE PAGAMENTOS S.A.", table=t) == "stoneco"  # index untouched
    assert "STONE PAGAMENTOS S.A." not in er.get_entity("stonex", table=t)["aliases"]


def test_accumulate_aliases_unknown_entity_is_noop():
    t = FakeTable()
    assert er.accumulate_aliases("ghost", ["Some Bank S.A."], table=t) == []


def test_load_alias_map_returns_raw_forms():
    t = FakeTable()
    er.put_entity("nubank", "Nubank", ["NUBANK", "NU"],
                  alias_forms=["NUBANK", " NU ", "TICKER:NU"], table=t)
    er.clear_cache()
    m = er.load_alias_map(table=t)
    assert m["nubank"] == ["NUBANK", " NU ", "TICKER:NU"]  # raw curated forms preserved
    er.clear_cache()
