import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import comparative


def _features():
    return {
        "as_of": "2026-08-23",
        "entities": [
            # outperformer: 0.8 vs cohort 0.4, std 0.1 -> z=+4.0
            {"entity": "hot", "label": "Hot", "score_mean": 0.8, "score_last": 0.8,
             "active_days": 5, "days_since_last": 1, "industries": ["banking"]},
            # underperformer: 0.15 -> z=-2.5
            {"entity": "cold", "label": "Cold", "score_mean": 0.15, "score_last": 0.15,
             "active_days": 5, "days_since_last": 1, "industries": ["banking"]},
            # near the mean: z=0 -> not an outlier
            {"entity": "mid", "label": "Mid", "score_mean": 0.42, "score_last": 0.42,
             "active_days": 5, "days_since_last": 1, "industries": ["banking"]},
            # stale: outlier value but not current -> excluded by recency
            {"entity": "stale", "label": "Stale", "score_mean": 0.9, "score_last": 0.9,
             "active_days": 5, "days_since_last": 40, "industries": ["banking"]},
        ],
        "cohorts": {"banking": {"members": 4, "score_mean": 0.4, "score_std": 0.1}},
    }


def test_nominate_flags_outlier_direction_and_swot():
    cands = {c["entity"]: c for c in comparative.nominate(_features())}
    assert set(cands) == {"hot", "cold"}          # mid near mean, stale not current
    assert cands["hot"]["direction"] == "outperform" and cands["hot"]["peer_z"] == 4.0
    assert cands["cold"]["direction"] == "underperform"
    # SWOT mapping: outperform -> competitor Strength (+); underperform -> Weakness (-)
    assert comparative.swot_hint(cands["hot"]) == {
        "dimension": "S", "sign": "+", "cohort": "banking", "peer_z": 4.0}
    assert comparative.swot_hint(cands["cold"])["dimension"] == "W"


def test_small_cohort_is_not_compared():
    f = _features()
    f["cohorts"]["banking"]["members"] = 2   # below MIN_PEERS
    assert comparative.nominate(f) == []


def test_emit_on_change_suppresses_recent_same_tier():
    f = _features()
    prior = [{"axis": "comparative", "entity": "hot", "run_date": "2026-08-21",
              "direction": "outperform", "peer_tier": 3}]
    got = [c["entity"] for c in comparative.nominate(
        f, recent_narratives=prior, as_of="2026-08-23")]
    assert "hot" not in got          # standing outlier within cooldown, same tier
    assert "cold" in got             # different entity still fires


def test_build_narrative_is_labeled_inference_and_capped():
    cand = comparative.nominate(_features())[0]  # the outperformer
    n = comparative.build_narrative(cand)
    assert n["axis"] == "comparative" and n["is_inference"] and n["mode"] == "derived"
    assert n["is_alert"] is False
    assert n["threat_score"] <= 0.55            # context, not a top alert
    assert n["swot_hint"]["dimension"] == "S"
    assert "ACIMA dos pares" in n["narrative"]
