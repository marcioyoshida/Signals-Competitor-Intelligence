import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import entity_registry as er
from src.synth.entities import detect_b3_tickers


# --- detector -------------------------------------------------------------
def test_detects_valid_b3_shapes_and_ignores_noise():
    text = "Itaú (ITUB4) e Bradesco BBDC4 sobem; BDR da Nubank ROXO34; SANB11 unit. Não: ABCD9, R2D2, ABC1."
    got = detect_b3_tickers(text)
    assert got == ["ITUB4", "BBDC4", "ROXO34", "SANB11"]  # order-stable, ABCD9/ABC1 excluded


def test_detect_empty():
    assert detect_b3_tickers("") == []
    assert detect_b3_tickers(None) == []


# --- assignment (backfill) ------------------------------------------------
class FakeTable:
    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        it = self.items.get(Key["pk"])
        return {"Item": it} if it is not None else {}

    def put_item(self, Item):
        self.items[Item["pk"]] = Item

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}


def test_assign_ticker_sets_field_and_makes_it_resolvable():
    t = FakeTable()
    er.put_entity("itau", "Itaú", ["Itaú", "Itaú Unibanco"], industries=["banking"],
                  confidence="curated", table=t)
    assert er.assign_ticker("itau", "ITUB4", table=t) is True
    e = er.get_entity("itau", table=t)
    assert e["ticker"] == "ITUB4"
    assert "ITUB4" in e["alias_forms"]
    # the ticker is now an ALIAS# index entry -> resolvable
    assert er.resolve_by_alias("ITUB4", table=t) == "itau"
    # idempotent
    assert er.assign_ticker("itau", "ITUB4", table=t) is False


def test_assign_ticker_missing_entity_is_noop():
    t = FakeTable()
    assert er.assign_ticker("ghost", "GHOST3", table=t) is False


def test_backfill_only_touches_existing_entities():
    t = FakeTable()
    er.put_entity("bb", "Banco do Brasil", ["Banco do Brasil"], industries=["banking"],
                  confidence="curated", table=t)
    changed = dict(er.backfill_tickers(table=t))
    assert changed.get("bb") == "BBAS3"          # existing -> assigned
    assert "itau" not in changed                  # not in table -> skipped
    assert er.get_entity("bb", table=t)["ticker"] == "BBAS3"


# --- ownership classification (ADR-013) -----------------------------------
def test_classify_ownership_curated_and_derived():
    # curated overrides
    assert er.classify_ownership({"entity_id": "caixa"}) == "governmental"
    assert er.classify_ownership({"entity_id": "bb"}) == "mixed"
    # derived: listed (ticker/fatos) -> public; else private
    assert er.classify_ownership({"entity_id": "itau", "ticker": "ITUB4"}) == "public"
    assert er.classify_ownership({"entity_id": "acme", "fatos_term": "ACME SA"}) == "public"
    assert er.classify_ownership({"entity_id": "picpay"}) == "private"


def test_backfill_ownership_and_ticker_preserve():
    t = FakeTable()
    er.put_entity("bb", "Banco do Brasil", ["Banco do Brasil"], industries=["banking"],
                  confidence="curated", table=t)
    er.put_entity("picpay", "PicPay", ["PicPay"], confidence="curated", table=t)
    changed = dict(er.backfill_ownership(table=t))
    assert changed["bb"] == "mixed" and changed["picpay"] == "private"
    assert er.get_entity("bb", table=t)["ownership"] == "mixed"
    # a later ticker re-upsert must PRESERVE ownership (not drop it)
    er.assign_ticker("bb", "BBAS3", table=t)
    assert er.get_entity("bb", table=t)["ownership"] == "mixed"


def test_certifications_set_and_attrs():
    t = FakeTable()
    er.put_entity("itau", "Itaú", ["Itaú"], industries=["banking"], ticker="ITUB4",
                  confidence="curated", table=t)
    assert er.set_certifications("itau", ["ISO 27001", "PCI-DSS"], table=t) is True
    assert er.set_certifications("itau", ["ISO 27001", "PCI-DSS"], table=t) is False  # idempotent
    attrs = er.list_entity_attributes(table=t)
    assert attrs["itau"]["ownership"] == "public"
    assert attrs["itau"]["certifications"] == ["ISO 27001", "PCI-DSS"]
    assert attrs["itau"]["ticker"] == "ITUB4"
