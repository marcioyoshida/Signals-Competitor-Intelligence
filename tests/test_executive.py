"""ADR 021 §D/§G — the four enriched officer blocks (CSO/CRO/CCO/CPO): derivation, industry
scoping, and the defamation guardrail that keeps unconfirmed (#33-risk) distress off the board."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import executive


def _feed():
    return {
        "generated_at": "2026-09-04T20:00:00+00:00", "as_of": "2026-09-04",
        "dates": ["2026-08-22", "2026-08-23", "2026-08-24", "2026-08-25", "2026-08-26",
                  "2026-08-27", "2026-08-28", "2026-08-29", "2026-08-30", "2026-08-31",
                  "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"],
        "industry_options": [{"slug": "banking", "display_name": "Banking"},
                             {"slug": "fintech", "display_name": "Fintech"}],
        "industries": [{"slug": "banking", "display_name": "Banking", "covered": True,
                        "coverage_gap": False, "low_volume": False, "narratives": 175, "active_entities": 16},
                       {"slug": "fintech", "display_name": "Fintech", "covered": True,
                        "coverage_gap": True, "low_volume": False, "narratives": 20, "active_entities": 3}],
        "entities": [{"entity": "itau", "label": "Itaú Unibanco"}],
        "entity_attrs": {"itau": {"label": "Itaú", "industries": ["banking"],
                                  "radar": {"tier": "official", "score": 0.9}},
                         "bradesco": {"label": "Bradesco", "industries": ["banking"]}},
        "distress": [
            {"entity": "bb", "kind": "recuperacao_judicial", "label": "RJ", "confidence": "reported"},
            {"entity": "digio", "kind": "recuperacao_judicial", "label": "RJ", "confidence": "confirmed"},
        ],
        "reputation": [{"id": "rep:bradesco", "entity": "bradesco", "company": "BRADESCO",
                        "rank": 1, "index": 84.9, "category": "Bancos", "period": "2026-T2"}],
        "coverage_gaps": [{"id": "g1", "question": "quais bancos têm rating ESG?", "count": 2,
                           "status": "open", "reason": "no-grounding", "triage": {"class": "ingestion_gap"}}],
        "reviews": [{"review_id": "discovery:vinci", "kind": "discovery", "proposed": "VINCI",
                     "reason": "name_collision", "confidence": "cnpj"}],
        "regulatory_coverage": {"summary": {"segments": 15}, "entity_covered": [], "signal_only": [], "gap": []},
        "integrity": {"findings": [{"id": "i1", "kind": "card_primary_absent", "severity": "med",
                                    "summary": "x", "entity_id": "caixa", "card_id": "c1"},
                                   {"id": "i2", "kind": "fund_alias", "severity": "high",
                                    "summary": "y", "entity_id": "btg", "card_id": "c2"}],
                      "counts": {}, "total": 2},
        "feed": [
            {"id": "n1", "date": "2026-09-04", "entity": "itau", "entity_label": "Itaú",
             "kind": "entity_fusion", "industries": ["banking"], "is_alert": True,
             "threat_score": 0.9, "lenses": ["news"], "narrative": "Itaú avança em adquirência."},
            {"id": "n2", "date": "2026-08-24", "entity": "itau", "entity_label": "Itaú",
             "kind": "entity_fusion", "industries": ["banking"], "is_alert": False, "threat_score": 0.3,
             "narrative": "Itaú antiga."},
            {"id": "n3", "date": "2026-09-03", "entity": "nubank", "entity_label": "Nubank",
             "kind": "entity_fusion", "industries": ["fintech"], "is_alert": False, "threat_score": 0.6,
             "narrative": "Nubank cresce."},
            {"id": "r1", "date": "2026-09-04", "entity": None, "entity_label": "Regulatório",
             "kind": "regulatory_lifecycle", "domain": "Crédito", "is_alert": True, "threat_score": 0.8,
             "affected_industries": ["banking", "fintech"], "n_changes": 2,
             "changes": [{"art": "1", "verb": "altera"}],
             "change_record": {"change": "Alteração", "blast_radius": {"score": 1.0, "band": "market", "n_entities": 194},
                               "difficulty": {"band": "medium"}, "impact": "revisar processos"}},
        ],
    }


def test_build_executive_has_four_officers_and_shared_sectors():
    ex = executive.build_executive(_feed())
    assert ex["officers"] == ["cso", "cro", "cco", "cpo"]
    assert {s["slug"] for s in ex["sectors"]} == {"banking", "fintech"}
    for off in ex["officers"]:
        assert ex[off]["panels"] and ex[off]["by_industry"]["__all__"]


def test_threat_is_normalized_to_0_100():
    assert executive._threat({"threat_score": 0.9}) == 90
    assert executive._threat({"threat_score": 80}) == 80


def test_cso_confirmed_only_distress_and_named_watch():
    cso = executive.build_executive(_feed())["cso"]
    assert cso["by_industry"]["__all__"]["distress"] == 1  # bb (reported) dropped, digio kept
    watch = [r for r in cso["panels"]["recommendations"]
             if r["action"] == "open_watch" and r["horizon"] == "imediato"]
    assert watch and watch[0]["entity"] == "itau"  # not the r1 regulatory card


def test_cso_industry_scoping():
    cso = executive.build_executive(_feed())["cso"]
    assert cso["by_industry"]["banking"]["n_cards"] < cso["by_industry"]["__all__"]["n_cards"]


def test_cro_impact_sorts_by_blast_and_surfaces_changes():
    cro = executive.build_executive(_feed())["cro"]
    assert cro["by_industry"]["__all__"]["n_reg"] >= 1
    assert cro["panels"]["impact"] and cro["panels"]["impact"][0]["blast_band"] == "market"
    assert cro["panels"]["changes"] and cro["panels"]["changes"][0]["n_changes"] == 2


def test_cco_risk_register_and_reputation():
    cco = executive.build_executive(_feed())["cco"]
    assert cco["by_industry"]["__all__"]["n_integrity"] == 2
    assert cco["by_industry"]["__all__"]["n_high"] == 1
    kinds = {r["kind"] for r in cco["panels"]["risk_register"]}
    assert "reputacao" in kinds and "integridade" in kinds  # distress=0 here (both filtered/none)
    assert cco["panels"]["reputation"][0]["label"] == "BRADESCO"
    assert cco["panels"]["recommendations"][0]["action"] == "run_integrity_audit"


def test_cpo_coverage_blindspots_discovery_radar():
    cpo = executive.build_executive(_feed())["cpo"]
    a = cpo["by_industry"]["__all__"]
    assert a["n_gaps"] == 1 and a["n_reviews"] == 1 and a["n_coverage_gap"] == 1
    assert len(cpo["panels"]["coverage_map"]) == 2
    assert cpo["panels"]["blind_spots"][0]["question"].startswith("quais bancos")
    assert cpo["panels"]["discovery"][0]["proposed"] == "VINCI"
    assert cpo["panels"]["radar"].get("official") == 1


def test_discovery_industries_derived_from_hint_source_for_scoping():
    # §G CPO scoping: discovery proposals get an industry from their hint SOURCE so the
    # dashboard can filter them by sector (fiagro→agri-funds, bcb class→its industry).
    assert executive._discovery_industries("cvm_fiagro cnpj=1 ticker=X owner=y") == ["agri-funds"]
    assert executive._discovery_industries("bcb_consorcio cnpj=1 owner=y") == ["consorcio"]
    assert executive._discovery_industries("bcb Crédito Direto (SCD) cnpj=48529228") == ["fintech"]
    assert executive._discovery_industries("bcb Banco cnpj=1") == ["banking"]
    assert executive._discovery_industries("news_keyword_harvest:ner foo") == []  # untagged


def test_flow_routes_reg_change_to_cro_with_handoff_to_cco():
    ex = executive.build_executive(_feed())
    flow = ex["flow"]
    assert flow, "expected trajectories"
    reg = [t for t in flow if t["trigger"] == "mudanca_regulatoria"]
    assert reg and reg[0]["officer"] == "cro"
    assert reg[0]["handoff"] == "cco"  # r1 blast band = market → hand off to compliance
    assert reg[0]["evidence_ids"] == ["r1"] and reg[0]["action_ref"] == "open_watch"


def test_flow_trajectory_ids_are_stable():
    a = executive.build_executive(_feed())["flow"]
    b = executive.build_executive(_feed())["flow"]
    assert [t["id"] for t in a] == [t["id"] for t in b]  # stable → decisions can link back
    assert all(t["id"].startswith("traj-") for t in a)


def test_flow_sorted_by_severity():
    flow = executive.build_executive(_feed())["flow"]
    rank = {"crit": 0, "high": 1, "med": 2}
    sev = [rank[t["severity"]] for t in flow]
    assert sev == sorted(sev)


def test_metrics_attached_from_decisions():
    ex = executive.build_executive(_feed(), decisions=[
        {"officer": "cso", "verdict": "aprovado", "outcome": "favoravel", "industry": "banking"}])
    assert ex["metrics"]["n_decisions"] == 1 and ex["metrics"]["ets_feedback"] == 10.0


def test_recommendations_map_to_catalog_actions_for_all_officers():
    ex = executive.build_executive(_feed())
    allowed = {"flag_entity", "open_watch", "curate_belief", "propose_vertical",
               "resolve_review", "run_integrity_audit"}
    for off in ex["officers"]:
        for r in ex[off]["panels"]["recommendations"]:
            assert r["action"] in allowed
            assert r["horizon"] in ("imediato", "30d", "90d", "estrategico")
            assert r["officer"] == off
