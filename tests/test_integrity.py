"""ADR 018 Phase 3 — integrity audit detectors."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import integrity


def test_institution_leaf_pollution_flagged_only_from_automated_source():
    ents = [
        {"entity_id": "btg", "industries": ["investment-banking", "agri-funds"],
         "_prov": {"industries": {"source": "enrich"}}},                       # polluted
        {"entity_id": "bb", "industries": ["banking", "agri-funds"],
         "_prov": {"industries": {"source": "curated"}}},                      # curated -> NOT a finding
        {"entity_id": "fund", "industries": ["agri-funds"]},                   # pure fund -> ok
    ]
    kinds = {f["kind"]: f for f in integrity.audit_registry(ents)}
    assert kinds["institution_leaf_pollution"]["entity_id"] == "btg"
    assert kinds["institution_leaf_pollution"]["safe_fix"] is True
    assert not any(f["entity_id"] == "bb" for f in integrity.audit_registry(ents))


def test_fund_alias_and_unbacked_and_inversion():
    ents = [
        {"entity_id": "cap", "industries": ["asset-management"],
         "ticker": "CPAC11", "aliases": ["CAP", "CPTA11"]},                    # fund alias
        {"entity_id": "x", "industries": ["banking"], "confidence": "cnpj", "cnpj_roots": []},
        {"entity_id": "kinea", "industries": ["agri-funds"]},
        {"entity_id": "kfund", "industries": ["agri-funds"], "parent": "kinea"},  # parent is leaf
    ]
    kinds = {f["kind"] for f in integrity.audit_registry(ents)}
    assert {"fund_alias_on_institution", "unbacked_cnpj", "parent_inversion"} <= kinds


def test_card_primary_absent():
    ents = [{"entity_id": "sportingbet", "aliases": ["SPORTINGBET"], "display_name": "Sportingbet"},
            {"entity_id": "stone", "aliases": ["STONECO", "STONE"], "display_name": "Stone"}]
    feed = {"feed": [
        {"id": "c1", "kind": "competitor:news", "entity": "sportingbet",
         "narrative": "A STONECO divulgou resultados no SEC."},               # primary absent, names stone
        {"id": "c2", "kind": "competitor:news", "entity": "stone",
         "narrative": "A Stone reportou lucro."},                             # primary present -> ok
    ]}
    findings = integrity.audit_feed(feed, ents)
    ids = {f["card_id"] for f in findings}
    assert "c1" in ids and "c2" not in ids


def test_audit_sorts_by_severity_and_counts():
    ents = [{"entity_id": "btg", "industries": ["investment-banking", "agri-funds"],
             "_prov": {"industries": {"source": "discovery"}}}]
    rep = integrity.audit({"feed": []}, ents)
    assert rep["total"] == 1 and rep["counts"]["institution_leaf_pollution"] == 1
    assert rep["findings"][0]["severity"] == "high"
