"""#74 / R2 — register-VERIFIED certifications for tracked entities (bcb_autorizacoes)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import bcb_autorizacoes as ba
from src.synth import entity_registry as er


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        self.items[Item["pk"]] = Item

    def get_item(self, Key):
        it = self.items.get(Key["pk"])
        return {"Item": it} if it is not None else {}


def _rows():
    return [
        {"cnpj": "12.345.678/0001-00", "name": "Banco X", "entity_type": "Banco Múltiplo",
         "license_class": "Banco"},
        {"cnpj": "12345678000199", "name": "Banco X IP", "entity_type": "Instituição de Pagamento",
         "license_class": "Instituição de Pagamento"},
        {"cnpj": None, "name": "sem cnpj", "license_class": "Banco"},  # unjoinable → skipped
    ]


def test_certification_label_is_verified_string():
    assert ba.certification_label({"license_class": "Banco"}) == "BCB · Banco · em funcionamento"
    assert ba.certification_label({}) == "BCB · instituição autorizada · em funcionamento"


def test_certifications_by_cnpj_groups_by_root_and_skips_missing():
    m = ba.certifications_by_cnpj(_rows())
    # both rows share the 8-digit root → unioned; the no-CNPJ row is dropped
    assert set(m.keys()) == {"12345678"}
    assert m["12345678"] == {"BCB · Banco · em funcionamento",
                             "BCB · Instituição de Pagamento · em funcionamento"}


def test_apply_stamps_structured_certifications_on_resolved_entity():
    t = FakeTable()
    er.put_entity("banco_x", "Banco X", ["Banco X"], cnpj_roots=["12345678"],
                  industries=["banking"], source="fixture", table=t)
    changed = ba.apply_verified_certifications(_rows(), table=t)
    assert changed == ["banco_x"]
    e = er.get_entity("banco_x", table=t)
    assert set(e["certifications"]) == {"BCB · Banco · em funcionamento",
                                        "BCB · Instituição de Pagamento · em funcionamento"}
    assert e["_prov"]["certifications"]["source"] == "structured"  # ADR-018 provenance
    # idempotent: a second apply changes nothing
    assert ba.apply_verified_certifications(_rows(), table=t) == []


def test_structured_cannot_demote_curated_certifications():
    t = FakeTable()
    er.put_entity("banco_x", "Banco X", ["Banco X"], cnpj_roots=["12345678"],
                  industries=["banking"], source="fixture", table=t)
    # an analyst curated a cert list → protected
    assert er.set_certifications("banco_x", ["ISO 27001 (curado)"], source="curated", table=t)
    changed = ba.apply_verified_certifications(_rows(), table=t)
    assert changed == []  # structured write is refused against a curated list
    assert er.get_entity("banco_x", table=t)["certifications"] == ["ISO 27001 (curado)"]


def test_unresolved_cnpj_is_a_noop():
    t = FakeTable()  # no entities → nothing resolves
    assert ba.apply_verified_certifications(_rows(), table=t) == []
