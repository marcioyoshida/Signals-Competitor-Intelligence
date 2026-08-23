import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import behavioral

AS_OF = "2026-08-23"


def _d(days_ago):
    return (dt.date.fromisoformat(AS_OF) - dt.timedelta(days=days_ago)).isoformat()


def _card(entity, text, days_ago, *, nid=None):
    return {"id": nid or f"competitor:{entity}", "entity": entity, "narrative": text,
            "threat_score": 0.6, "run_date": _d(days_ago), "lenses": ["news"],
            "citations": [{"url": f"https://ex/{entity}/{days_ago}"}]}


def _feat(entity, **kw):
    base = {"entity": entity, "label": entity.title(), "active_days": 5,
            "days_since_last": 1, "mean_gap_days": None, "cadence_regular": False}
    base.update(kw)
    return base


def test_multi_front_needs_three_event_types():
    narrs = [
        _card("brad", "aquisição da rival anunciada", 6),
        _card("brad", "novo processo judicial", 4),
        _card("brad", "lançamento de novo produto", 2),
    ]
    feats = {"entities": [_feat("brad")], "as_of": AS_OF}
    cands = {c["pattern"]: c for c in behavioral.nominate(feats, narrs, as_of=AS_OF)}
    assert "multi_front" in cands
    assert cands["multi_front"]["n_fronts"] == 3
    assert behavioral.swot_hint(cands["multi_front"]) == {
        "dimension": "S", "sign": "+", "pattern": "multi_front"}
    # only two fronts -> no multi_front
    narrs2 = [_card("x", "aquisição anunciada", 4), _card("x", "novo processo", 2)]
    feats2 = {"entities": [_feat("x")], "as_of": AS_OF}
    assert [c for c in behavioral.nominate(feats2, narrs2, as_of=AS_OF)
            if c["pattern"] == "multi_front"] == []


def test_drumbeat_needs_regular_frequent_cadence():
    feats = {"entities": [_feat("y", cadence_regular=True, mean_gap_days=5.0)], "as_of": AS_OF}
    cands = [c for c in behavioral.nominate(feats, [], as_of=AS_OF) if c["pattern"] == "drumbeat"]
    assert len(cands) == 1 and cands[0]["mean_gap_days"] == 5.0
    # irregular cadence -> no drumbeat
    feats2 = {"entities": [_feat("z", cadence_regular=False, mean_gap_days=5.0)], "as_of": AS_OF}
    assert [c for c in behavioral.nominate(feats2, [], as_of=AS_OF) if c["pattern"] == "drumbeat"] == []
    # regular but too sparse (gap beyond drumbeat window) -> no drumbeat
    feats3 = {"entities": [_feat("w", cadence_regular=True, mean_gap_days=40.0)], "as_of": AS_OF}
    assert [c for c in behavioral.nominate(feats3, [], as_of=AS_OF) if c["pattern"] == "drumbeat"] == []


def test_stale_entity_not_nominated():
    feats = {"entities": [_feat("y", cadence_regular=True, mean_gap_days=5.0, days_since_last=40)],
             "as_of": AS_OF}
    assert behavioral.nominate(feats, [], as_of=AS_OF) == []


def test_emit_on_change_suppresses_recent_pattern():
    feats = {"entities": [_feat("y", cadence_regular=True, mean_gap_days=5.0)], "as_of": AS_OF}
    prior = {"id": "behavioral-y-drumbeat", "axis": "behavioral", "entity": "y",
             "pattern": "drumbeat", "run_date": _d(2)}
    got = [c for c in behavioral.nominate(feats, [prior], as_of=AS_OF)]
    assert got == []   # within cooldown, same pattern


def test_build_narrative_is_labeled_and_capped():
    feats = {"entities": [_feat("brad")], "as_of": AS_OF}
    narrs = [_card("brad", "aquisição", 6), _card("brad", "processo judicial", 4),
             _card("brad", "lançamento de produto", 2)]
    c = behavioral.nominate(feats, narrs, as_of=AS_OF)[0]
    n = behavioral.build_narrative(c)
    assert n["axis"] == "behavioral" and n["is_inference"] and n["mode"] == "derived"
    assert n["is_alert"] is False and n["threat_score"] <= 0.5
    assert "Padrão de comportamento" in n["narrative"]
    assert n["swot_hint"]["dimension"] == "S"
