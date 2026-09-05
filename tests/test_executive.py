"""ADR 021 §D/§G — the CSO executive block: derivation, industry scoping, and the
defamation guardrail that keeps unconfirmed (#33-risk) distress off the board."""
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
        "entities": [{"entity": "itau", "label": "Itaú Unibanco"},
                     {"entity": "nubank", "label": "Nubank"}],
        "entity_attrs": {"itau": {"label": "Itaú", "industries": ["banking"]}},
        "distress": [
            {"entity": "bb", "kind": "recuperacao_judicial", "label": "Recuperação Judicial",
             "confidence": "reported", "latest_title": "Casas Bahia contra Banco do Brasil"},
            {"entity": "digio", "kind": "recuperacao_judicial", "label": "RJ",
             "confidence": "confirmed", "latest_title": "Digio pede recuperação"},
        ],
        "feed": [
            {"id": "n1", "date": "2026-09-04", "entity": "itau", "entity_label": "Itaú",
             "kind": "entity_fusion", "industries": ["banking"], "is_alert": True,
             "threat_score": 0.9, "lenses": ["news"], "narrative": "Itaú avança em adquirência."},
            {"id": "n2", "date": "2026-08-24", "entity": "itau", "entity_label": "Itaú",
             "kind": "entity_fusion", "industries": ["banking"], "is_alert": False,
             "threat_score": 0.3, "narrative": "Itaú notícia antiga."},
            {"id": "n3", "date": "2026-09-03", "entity": "nubank", "entity_label": "Nubank",
             "kind": "entity_fusion", "industries": ["fintech"], "is_alert": False,
             "threat_score": 0.7, "topics": ["concorrencia"], "narrative": "Nubank aquisição de fintech."},
            {"id": "r1", "date": "2026-09-04", "entity": None, "entity_label": "Regulatório",
             "kind": "regulatory_lifecycle", "domain": "Crédito", "is_alert": True,
             "threat_score": 0.8, "affected_industries": ["banking", "fintech"]},
        ],
    }


def test_build_executive_lists_only_cso_for_now():
    ex = executive.build_executive(_feed())
    assert ex["officers"] == ["cso"]
    assert "cso" in ex and ex["cso"]["panels"]


def test_threat_is_normalized_to_0_100():
    assert executive._threat({"threat_score": 0.9}) == 90
    assert executive._threat({"threat_score": 80}) == 80  # already 0-100 left alone


def test_sectors_carry_labels():
    cso = executive.build_cso(_feed())
    labels = {s["slug"]: s["label"] for s in cso["sectors"]}
    assert labels["banking"] == "Banking" and labels["fintech"] == "Fintech"


def test_industry_scoping_filters_aggregates():
    cso = executive.build_cso(_feed())
    bi = cso["by_industry"]
    assert bi["banking"]["n_cards"] < bi["__all__"]["n_cards"]
    # the fintech card (n3) is not counted under banking
    assert bi["banking"]["n_cards"] == 3  # n1, n2, r1(affected banking)


def test_confirmed_only_distress_keeps_misattribution_off_the_board():
    cso = executive.build_cso(_feed())
    # bb (reported, #33 counterparty mis-attribution) dropped; digio (confirmed) kept
    assert cso["by_industry"]["__all__"]["distress"] == 1
    recs = cso["panels"]["recommendations"]
    assert not any(r.get("entity") == "bb" for r in recs)
    assert any(r.get("entity") == "digio" for r in recs)


def test_watch_rec_targets_a_named_competitor_not_a_regulatory_card():
    recs = executive.build_cso(_feed())["panels"]["recommendations"]
    watch = [r for r in recs if r["action"] == "open_watch" and r["horizon"] == "imediato"]
    assert watch and watch[0]["entity"] == "itau"  # not the r1 regulatory card


def test_momentum_reflects_recent_minus_prior():
    cso = executive.build_cso(_feed())
    m = {x["entity"]: x for x in cso["panels"]["momentum"]}
    # itau: recent(0.9→90) vs prior(0.3→30) ⇒ +60
    assert m["itau"]["momentum"] == 60.0


def test_recommendations_map_to_catalog_actions():
    recs = executive.build_cso(_feed())["panels"]["recommendations"]
    assert {r["action"] for r in recs} <= {
        "flag_entity", "open_watch", "curate_belief", "propose_vertical", "resolve_review"}
    assert all(r.get("horizon") in ("imediato", "30d", "90d", "estrategico") for r in recs)
