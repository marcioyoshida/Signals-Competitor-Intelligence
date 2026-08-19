import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import feature_store as fs
from src.synth import longitudinal as lg


def _narr(entity, run_date, score, *, citations=None, source_ids=None, lenses=None):
    return {
        "entity": entity,
        "entity_label": entity.title(),
        "run_date": run_date,
        "threat_score": score,
        "lenses": lenses or ["regulatory"],
        "citations": citations or [],
        "source_ids": source_ids or [],
    }


def _break_card(entity, run_date, tier, direction):
    return {"entity": entity, "run_date": run_date, "axis": "longitudinal",
            "break_tier": tier, "direction": direction}


def test_escalation_break_nominated_with_driver_evidence():
    # btg: flat ~0.30 baseline, then a spike to 0.90 today (its own pattern breaks).
    narrs = [
        _narr("btg", "2026-08-10", 0.30),
        _narr("btg", "2026-08-12", 0.30),
        _narr("btg", "2026-08-14", 0.30),
        _narr("btg", "2026-08-16", 0.90,
              citations=[{"url": "https://bcb.gov.br/x", "source": "BCB"}],
              source_ids=["bcb:x"]),
    ]
    feats = fs.build_features(narrs, as_of="2026-08-16")
    cands = lg.nominate(feats, recent_narratives=narrs, as_of="2026-08-16")
    assert [c["entity"] for c in cands] == ["btg"]
    c = cands[0]
    assert c["direction"] == "escalation"
    assert c["score_z"] >= 2.0 and c["break_tier"] == 3
    # driver = the spike narrative, carried as evidence into the card
    card = lg.build_narrative(c)
    assert card["axis"] == "longitudinal" and card["is_inference"] is True
    assert card["is_alert"] is False and card["mode"] == "derived"
    assert card["direction"] == "escalation"
    assert card["citations"] == [{"url": "https://bcb.gov.br/x", "source": "BCB"}]
    assert card["source_ids"] == ["bcb:x"]
    assert card["threat_score"] == 0.9        # escalation reports the real level
    assert "z=+" in card["narrative"] and "Btg" in card["narrative"]


def test_cooling_break_scores_low():
    # itau: high ~0.85 baseline, then drops to 0.30 today.
    narrs = [
        _narr("itau", "2026-08-10", 0.85),
        _narr("itau", "2026-08-12", 0.85),
        _narr("itau", "2026-08-14", 0.88),
        _narr("itau", "2026-08-16", 0.30),
    ]
    feats = fs.build_features(narrs, as_of="2026-08-16")
    cands = lg.nominate(feats, recent_narratives=narrs, as_of="2026-08-16")
    assert cands and cands[0]["direction"] == "cooling" and cands[0]["score_z"] <= -2.0
    card = lg.build_narrative(cands[0])
    assert card["threat_score"] <= 0.35       # cooling is a low watch signal
    assert "recuo" in card["narrative"]


def test_stale_break_not_flagged_when_last_activity_old():
    # A break long ago (last active 6 days back) is not "current" -> skip.
    narrs = [
        _narr("stone", "2026-08-01", 0.30),
        _narr("stone", "2026-08-03", 0.30),
        _narr("stone", "2026-08-05", 0.30),
        _narr("stone", "2026-08-10", 0.90),   # spike, but 6 days stale on as_of
    ]
    feats = fs.build_features(narrs, as_of="2026-08-16")
    assert lg.nominate(feats, recent_narratives=narrs, as_of="2026-08-16") == []


def test_insufficient_baseline_skipped():
    # Only 3 active days (<4) -> not enough baseline for a robust break.
    narrs = [
        _narr("xp", "2026-08-12", 0.30),
        _narr("xp", "2026-08-14", 0.30),
        _narr("xp", "2026-08-16", 0.90),
    ]
    feats = fs.build_features(narrs, as_of="2026-08-16")
    assert lg.nominate(feats, recent_narratives=narrs, as_of="2026-08-16") == []


def test_small_absolute_move_not_a_break():
    # Tight baseline, but the absolute move is tiny -> not material.
    narrs = [
        _narr("nubank", "2026-08-10", 0.50),
        _narr("nubank", "2026-08-12", 0.50),
        _narr("nubank", "2026-08-14", 0.50),
        _narr("nubank", "2026-08-16", 0.56),   # +0.06 < MIN_ABS_MOVE
    ]
    feats = fs.build_features(narrs, as_of="2026-08-16")
    assert lg.nominate(feats, recent_narratives=narrs, as_of="2026-08-16") == []


def test_cooldown_suppresses_same_direction_unless_escalated():
    narrs = [
        _narr("btg", "2026-08-10", 0.30),
        _narr("btg", "2026-08-12", 0.30),
        _narr("btg", "2026-08-14", 0.30),
        _narr("btg", "2026-08-16", 0.90),      # z ~ tier 3 escalation
    ]
    feats = fs.build_features(narrs, as_of="2026-08-16")
    # prior escalation 1 day ago at same tier -> suppressed
    same = narrs + [_break_card("btg", "2026-08-15", tier=3, direction="escalation")]
    assert lg.nominate(feats, recent_narratives=same, as_of="2026-08-16") == []
    # prior escalation at a LOWER tier -> re-fires
    lower = narrs + [_break_card("btg", "2026-08-15", tier=1, direction="escalation")]
    assert [c["entity"] for c in lg.nominate(feats, recent_narratives=lower, as_of="2026-08-16")] == ["btg"]
    # a break card is not activity -> baseline still 0.30, spike still detected
    assert lg.nominate(feats, recent_narratives=lower, as_of="2026-08-16")[0]["score_last"] == 0.9


def test_retract_same_day_removes_only_normalized():
    deleted = []

    class FakeS3:
        def head_object(self, Bucket, Key):
            if Key not in store:
                raise KeyError(Key)
            return {}

        def delete_object(self, Bucket, Key):
            deleted.append(Key)
            store.discard(Key)

    store = {
        "narratives/2026-08-16/longitudinal-btg.json",
        "narratives/2026-08-16/longitudinal-itau.json",
    }
    out = lg._retract_same_day("b", FakeS3(), "2026-08-16", {"btg", "nubank"})
    assert out == ["btg"]                       # itau untouched (still breaking)
    assert deleted == ["narratives/2026-08-16/longitudinal-btg.json"]


def test_longitudinal_excluded_from_feature_activity():
    activity = [_narr("btg", "2026-08-16", 0.9)]
    brk = {"entity": "btg", "run_date": "2026-08-16", "axis": "longitudinal",
           "threat_score": 0.9, "is_inference": True, "mode": "derived"}
    feats = fs.build_features(activity + [brk], as_of="2026-08-16")
    btg = {e["entity"]: e for e in feats["entities"]}["btg"]
    assert btg["active_days"] == 1              # break card ignored
