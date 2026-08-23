import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import predictive


def _act(nid, ent, *, date, text="atividade", threat=0.5):
    return {"id": nid, "kind": "entity_fusion", "entity": ent, "entities": [ent],
            "run_date": date, "narrative": text, "threat_score": threat, "lenses": ["news"]}


def test_history_depth_span():
    narr = [_act("a", "x", date="2026-04-01"), _act("b", "x", date="2026-08-01")]
    assert predictive.history_depth_days(narr) == 122
    assert predictive.history_depth_days([_act("a", "x", date="2026-08-01")]) == 0


def test_momentum_buildup_rule():
    features = {"entities": [
        {"entity": "nubank", "label": "Nubank", "score_z": 2.0, "cadence_regular": True},
        {"entity": "itau", "label": "Itaú", "score_z": 0.1, "cadence_regular": True},
    ]}
    inds = predictive.leading_indicators(features, [])
    sigs = {(i["entity"], i["signal"]) for i in inds}
    assert ("nubank", "momentum_buildup") in sigs
    assert ("itau", "momentum_buildup") not in sigs   # z below threshold


def test_launch_precursor_rule():
    # an authorization event + expansion event on the entity -> launch precursor
    narr = [{"id": "n1", "kind": "entity_fusion", "entity": "c6", "entities": ["c6"],
             "run_date": "2026-08-23", "lenses": ["news"],
             "narrative": "C6 recebeu autorização do banco central e prepara expansão internacional."}]
    features = {"entities": [{"entity": "c6", "label": "C6", "score_z": 0.0,
                             "cadence_regular": False}]}
    inds = predictive.leading_indicators(features, narr)
    assert any(i["signal"] == "launch_precursor" for i in inds)


def test_build_card_is_labeled_inference():
    card = predictive.build_card({"entity": "nubank", "label": "Nubank",
                                  "signal": "momentum_buildup", "score_z": 2.0,
                                  "horizon_days": 14})
    assert card["axis"] == "predictive" and card["is_inference"] is True
    assert card["threat_score_note"].endswith("unvalidated")
    assert "NÃO previsão validada" in card["narrative"]


def test_derived_axis_registered():
    from src.synth import feature_store
    assert "predictive" in feature_store.DERIVED_AXES
