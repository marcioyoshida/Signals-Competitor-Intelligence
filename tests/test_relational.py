import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import relational


def _thematic(theme, entities, *, date, nid=None):
    return {"id": nid or f"thematic-{theme}", "axis": "thematic", "entity": None,
            "run_date": date, "entities": entities, "theme_display": theme,
            "swot_hint": {"theme": theme, "entities": entities}}


def _activity(nid, entities, *, date, lenses=None, event=None):
    n = {"id": nid, "kind": "entity_fusion", "entity": entities[0], "entities": entities,
         "run_date": date, "lenses": lenses or ["news"],
         "narrative": "sinal de atividade"}
    if event:
        n["event_type"] = event
        n["narrative"] = f"{event} envolvendo as partes"
    return n


def test_niche_theme_convergence_is_proposed_not_carded():
    narr = [
        _thematic("crypto", ["bradesco", "binance", "coinbase"], date="2026-08-20"),
        _thematic("crypto", ["bradesco", "binance", "coinbase"], date="2026-08-23"),
    ]
    edges = relational.build_graph(narr)
    nom = relational.nominate(edges)
    assert nom["factual"] == []                      # convergence never auto-cards
    kinds = {p["relation"] for p in nom["proposals"]}
    assert kinds == {"convergence"}
    bb = [p for p in nom["proposals"] if {p["a"], p["b"]} == {"binance", "bradesco"}][0]
    assert bb["n_dates"] == 2 and "crypto" in bb["themes"]


def test_crowded_theme_is_not_a_relationship():
    big = ["bb", "bradesco", "btg", "caixa", "itau", "nubank", "santander"]  # > NICHE_MAX
    narr = [_thematic("quarterly_results", big, date="2026-08-20"),
            _thematic("quarterly_results", big, date="2026-08-23")]
    edges = relational.build_graph(narr)
    assert edges == {}                               # crowded arena -> no edges


def test_convergence_needs_multiple_dates():
    narr = [_thematic("betting", ["betano", "pixbet"], date="2026-08-23")]  # one date only
    nom = relational.nominate(relational.build_graph(narr))
    assert nom["proposals"] == []


def test_co_mention_from_activity_is_factual_card():
    narr = [_activity(f"news-{i}", ["stone", "pagseguro"], date=f"2026-08-2{i}")
            for i in (1, 3)]  # 2 signals, 2 dates
    edges = relational.build_graph(narr)
    nom = relational.nominate(edges)
    assert len(nom["factual"]) == 1
    card = relational.build_card(nom["factual"][0])
    assert card["subject_type"] == "pair"
    assert card["axis"] == "relational" and card["is_inference"] is True
    assert set(card["entities"]) == {"pagseguro", "stone"}


def test_litigation_co_party_is_dispute_proposal():
    narr = [{"id": "dou-1", "kind": "entity_fusion", "entity": "itau",
             "entities": ["itau", "nubank"], "run_date": "2026-08-23", "lenses": ["dou"],
             "narrative": "Processo judicial: ação movida envolvendo as partes."}]
    edges = relational.build_graph(narr)
    nom = relational.nominate(edges)
    disputes = [e for e in nom["proposals"] if e["relation"] == "dispute"]
    assert len(disputes) == 1
    prop = relational.build_proposal(disputes[0], run_date="2026-08-23")
    assert prop["status"] == "pending" and prop["kind"] == "dispute"  # never auto-published
    # dispute must NOT also appear as a factual card
    assert all(e["relation"] != "dispute" for e in nom["factual"])


def test_merge_proposals_preserves_human_status():
    existing = [{"id": "convergence:a:b", "status": "accepted", "created": "2026-08-20",
                 "last_seen": "2026-08-20"}]
    fresh = [{"id": "convergence:a:b", "status": "pending", "created": "2026-08-23",
              "last_seen": "2026-08-23"},
             {"id": "convergence:a:c", "status": "pending", "created": "2026-08-23",
              "last_seen": "2026-08-23"}]
    out = {p["id"]: p for p in relational.merge_proposals(existing, fresh)}
    assert out["convergence:a:b"]["status"] == "accepted"
    assert out["convergence:a:b"]["created"] == "2026-08-20"
    assert out["convergence:a:c"]["status"] == "pending"


def test_derived_axis_registered():
    from src.synth import feature_store
    assert "relational" in feature_store.DERIVED_AXES
    # a relational card must not count as entity activity
    card = {"axis": "relational", "entity": None, "mode": "derived"}
    assert feature_store.is_activity_narrative(card) is False
