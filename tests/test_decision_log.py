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


# --- DEC-2: durable per-tenant TDR baseline -------------------------------------
def test_tdr_baseline_roundtrip_and_none_when_unset(tbl):
    assert decision_log.get_tdr_baseline(table=tbl) is None      # never recorded → None (honest)
    it = decision_log.set_tdr_baseline(16, actor="operator", table=tbl)
    assert it["baseline_hours"] == 16.0 and it["type"] == "config"
    assert decision_log.get_tdr_baseline(table=tbl) == 16.0


def test_tdr_baseline_rejects_non_positive(tbl):
    for bad in (0, -3, "x"):
        with pytest.raises(ValueError):
            decision_log.set_tdr_baseline(bad, actor="operator", table=tbl)


# --- DEC-6: decision -> action closure ------------------------------------------
def test_link_action_appends_trail(tbl):
    it = decision_log.record_decision(officer="cco", recommendation="Auditar", verdict="aprovado",
                                      actor="op", table=tbl)
    did = it["decision_id"]
    assert decision_log.link_action(did, intent="run_integrity_audit", outcome="applied",
                                    actor="op", table=tbl) is True
    stored = tbl.items[f"DECISION#{did}"]
    assert stored["actions"][0]["intent"] == "run_integrity_audit"
    assert stored["actions"][0]["outcome"] == "applied"
    assert decision_log.link_action("nope", intent="x", outcome="y", actor="op", table=tbl) is False


# --- DEC-5 (#98): decisions under ADR-018 governance (provenance + precedence + rollback) ----
@pytest.fixture
def journaled(monkeypatch):
    """In-memory stand-in for the OncaCurationLog journal, so the decision-governance logic
    (precedence + rollback reconstruction) is exercised without DynamoDB condition internals."""
    log: list[dict] = []
    _n = {"i": 0}

    def _log(entity_id, action, source, detail=None):
        _n["i"] += 1
        log.append({"entity_id": str(entity_id), "ts": f"2026-09-06T00:00:{_n['i']:02d}.000",
                    "action": action, "source": source, "detail": detail or {}})

    def _history(entity_id, *, limit=200):
        return [dict(h) for h in reversed(log) if h["entity_id"] == str(entity_id)][:limit]

    monkeypatch.setattr(decision_log._er, "_log", _log)
    monkeypatch.setattr(decision_log._er, "entity_history", _history)
    return log


def test_record_stamps_curated_provenance(tbl, journaled):
    it = decision_log.record_decision(officer="cso", recommendation="r", verdict="aprovado",
                                      actor="op", table=tbl)
    prov = tbl.items[f"DECISION#{it['decision_id']}"]["_prov"]
    assert prov["outcome"]["source"] == "curated" and prov["board_adopted"]["source"] == "curated"


def test_automated_outcome_cannot_demote_curated(tbl, journaled):
    it = decision_log.record_decision(officer="cso", recommendation="r", verdict="aprovado",
                                      actor="op", table=tbl)
    did = it["decision_id"]
    # human stamps a curated outcome
    decision_log.set_outcome(did, "favoravel", actor="exec", table=tbl)
    # an automated (inferred) writer tries to overwrite it → rejected, item unchanged
    res = decision_log.set_outcome(did, "desfavoravel", actor="bot", source="inferred", table=tbl)
    assert res["outcome"] == "favoravel"
    assert any(h["action"] == "blocked" and h["detail"]["field"] == "outcome" for h in journaled)


def test_curated_outcome_overwrites_inferred(tbl, journaled):
    it = decision_log.record_decision(officer="cso", recommendation="r", verdict="aprovado",
                                      actor="op", source="inferred", table=tbl)
    did = it["decision_id"]
    up = decision_log.set_outcome(did, "favoravel", actor="exec", source="curated", table=tbl)
    assert up["outcome"] == "favoravel" and up["_prov"]["outcome"]["source"] == "curated"


def test_rollback_outcome_restores_prior_value(tbl, journaled):
    it = decision_log.record_decision(officer="cso", recommendation="r", verdict="aprovado",
                                      actor="op", table=tbl)
    did = it["decision_id"]
    decision_log.set_outcome(did, "favoravel", actor="exec", table=tbl)
    bad = decision_log.set_outcome(did, "desfavoravel", actor="exec", table=tbl)
    cutoff = [h for h in journaled if h["detail"].get("new") == "desfavoravel"][0]["ts"]
    assert decision_log.rollback_decision_field(did, "outcome", cutoff, actor="admin", table=tbl) is True
    assert tbl.items[f"DECISION#{did}"]["outcome"] == "favoravel"


def test_rollback_rejects_unsupported_field(tbl, journaled):
    it = decision_log.record_decision(officer="cso", recommendation="r", verdict="aprovado",
                                      actor="op", table=tbl)
    with pytest.raises(ValueError):
        decision_log.rollback_decision_field(it["decision_id"], "verdict", "2026-01-01", actor="a")
