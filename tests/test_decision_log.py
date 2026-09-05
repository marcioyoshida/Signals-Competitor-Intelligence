"""ADR 021 §D Step 1 — the append-only decision-capture store (OncaDecisionLog)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.synth import decision_log


class _FakeTable:
    """Minimal in-memory DynamoDB Table stand-in (get_item/put_item + a scan shim)."""
    def __init__(self):
        self.items: dict[str, dict] = {}

    def put_item(self, Item):
        self.items[Item["pk"]] = dict(Item)

    def get_item(self, Key):
        it = self.items.get(Key["pk"])
        return {"Item": dict(it)} if it else {}


@pytest.fixture
def tbl(monkeypatch):
    t = _FakeTable()
    # _scan_type reads from _er._scan_type — stub it to our fake store
    monkeypatch.setattr(decision_log._er, "_scan_type",
                        lambda table, type_: [v for v in t.items.values() if v.get("type") == type_])
    return t


def test_record_decision_appends_with_pending_outcome(tbl):
    it = decision_log.record_decision(
        officer="cso", recommendation="Abrir watch em Itaú", verdict="aprovado",
        actor="operator", industry="banking", action_ref="open_watch", evidence_id="n1", table=tbl)
    assert it["decision_id"] and it["verdict"] == "aprovado" and it["outcome"] == "pendente"
    stored = tbl.items[f"DECISION#{it['decision_id']}"]
    assert stored["type"] == "decision" and stored["officer"] == "cso" and stored["industry"] == "banking"


def test_record_decision_rejects_bad_verdict(tbl):
    with pytest.raises(ValueError):
        decision_log.record_decision(officer="cso", recommendation="x", verdict="talvez",
                                     actor="op", table=tbl)


def test_record_decision_rejects_empty_recommendation(tbl):
    with pytest.raises(ValueError):
        decision_log.record_decision(officer="cso", recommendation="  ", verdict="aprovado",
                                     actor="op", table=tbl)


def test_set_outcome_updates_existing(tbl):
    it = decision_log.record_decision(officer="cso", recommendation="r", verdict="aprovado",
                                      actor="op", table=tbl)
    up = decision_log.set_outcome(it["decision_id"], "favoravel", actor="exec", note="deu certo", table=tbl)
    assert up["outcome"] == "favoravel" and up["outcome_note"] == "deu certo" and up["outcome_by"] == "exec"


def test_set_outcome_missing_decision_returns_none(tbl):
    assert decision_log.set_outcome("nope", "favoravel", actor="op", table=tbl) is None


def test_set_outcome_rejects_bad_outcome(tbl):
    it = decision_log.record_decision(officer="cso", recommendation="r", verdict="aprovado",
                                      actor="op", table=tbl)
    with pytest.raises(ValueError):
        decision_log.set_outcome(it["decision_id"], "otimo", actor="op", table=tbl)


def test_append_reference_is_idempotent_per_url(tbl):
    it = decision_log.record_decision(officer="cso", recommendation="r", verdict="aprovado",
                                      actor="op", table=tbl)
    did = it["decision_id"]
    assert decision_log.append_reference(did, "https://x/1", officer="cso", table=tbl) is True
    assert decision_log.append_reference(did, "https://x/1", table=tbl) is False  # dup
    assert len(tbl.items[f"DECISION#{did}"]["references"]) == 1


def test_list_decisions_filters_and_sorts(tbl):
    decision_log.record_decision(officer="cso", recommendation="a", verdict="aprovado",
                                 actor="op", industry="banking", table=tbl)
    decision_log.record_decision(officer="cro", recommendation="b", verdict="rejeitado",
                                 actor="op", industry="seguros", table=tbl)
    assert len(decision_log.list_decisions(table=tbl)) == 2
    assert len(decision_log.list_decisions(officer="cso", table=tbl)) == 1
    assert len(decision_log.list_decisions(industry="seguros", table=tbl)) == 1
