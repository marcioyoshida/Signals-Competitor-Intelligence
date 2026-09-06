"""ADR 021 §E — Decision-Trust metrics: honest, only what OncaDecisionLog holds."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import decision_metrics as dm


def _d(officer, verdict, outcome, industry=None):
    return {"officer": officer, "verdict": verdict, "outcome": outcome, "industry": industry}


def test_empty():
    m = dm.compute_metrics([])
    assert m["n_decisions"] == 0 and m["ets"] is None and m["tdr"] is None
    assert m["ets_feedback"] is None  # no resolved decisions


def test_rates_and_feedback_component():
    ds = [_d("cso", "aprovado", "favoravel", "banking"), _d("cso", "aprovado", "pendente", "banking"),
          _d("cro", "rejeitado", "neutro", "fintech")]
    m = dm.compute_metrics(ds)
    assert m["n_decisions"] == 3 and m["n_approved"] == 2
    assert m["approval_rate"] == round(2/3, 3)
    assert m["n_resolved"] == 2 and m["influence_rate"] == round(2/3, 3)
    assert m["favorable_rate"] == 0.5 and m["ets_feedback"] == 5.0  # 1 favoravel of 2 resolved
    assert m["outcome_mix"] == {"favoravel": 1, "desfavoravel": 0, "neutro": 1, "pendente": 1}


def test_ets_is_partial_composite_board_and_tdr_none():
    # With a resolved decision, Feedback+Influence are measured → ets is a PARTIAL composite;
    # Board adoption stays unmeasured (None) and tdr stays None (no fabrication).
    m = dm.compute_metrics([_d("cso", "aprovado", "favoravel")])
    assert m["ets"] is not None and 0 <= m["ets"] <= 10 and "parcial" in m["ets_note"]
    assert m["ets_components"]["board"] is None
    assert m["ets_components"]["feedback"] == 10.0 and m["ets_components"]["influence"] == 10.0
    assert m["ets_components"]["engagement"] is None  # no engagement supplied
    assert m["tdr"] is None and "baseline" in m["tdr_note"]


def test_engagement_folds_into_ets():
    roll = {"n_interest": 25}  # 25/50 → engagement 5.0
    m = dm.compute_metrics([_d("cso", "aprovado", "favoravel")], engagement=roll)
    assert m["ets_components"]["engagement"] == 5.0
    # ets = (0.40*10 + 0.25*10 + 0.20*5) / 0.85 (no board component yet → renormalized)
    assert m["ets"] == round((0.40 * 10 + 0.25 * 10 + 0.20 * 5) / 0.85, 1)
    assert m["ets_full"] is False


def test_tdr_honest_three_states():
    d = {**_d("cso", "aprovado", "favoravel"),
         "started_at": "2026-09-05T10:00:00+00:00", "created_at": "2026-09-05T14:00:00+00:00"}  # 4h
    # (1) no baseline → None, honest note
    assert dm.compute_metrics([d])["tdr"] is None
    # (2) baseline 16h + measured 4h → 75% (the ADR example)
    m = dm.compute_metrics([d], tdr_baseline_hours=16)
    assert m["tdr"] == 75.0 and m["tdr_after_hours"] == 4.0 and m["tdr_baseline_hours"] == 16
    # (3) baseline but no timed decision → None ("medindo"), never fabricated
    m2 = dm.compute_metrics([_d("cso", "aprovado", "favoravel")], tdr_baseline_hours=16)
    assert m2["tdr"] is None and "medindo" in m2["tdr_note"]


def test_board_component_completes_the_full_composite():
    d = {**_d("cso", "aprovado", "favoravel"), "board_adopted": True, "board_at": "2026-09-05"}
    m = dm.compute_metrics([d], engagement={"n_interest": 25})
    assert m["n_board_flagged"] == 1 and m["board_rate"] == 1.0
    assert m["ets_components"]["board"] == 10.0
    assert m["ets_full"] is True  # all four components measured
    assert m["ets"] == round(0.40 * 10 + 0.25 * 10 + 0.20 * 5 + 0.15 * 10, 1) == 9.0
    assert "composto" in m["ets_note"]


def test_per_officer_and_industry_slices():
    ds = [_d("cso", "aprovado", "favoravel", "banking"), _d("cro", "aprovado", "desfavoravel", "seguros")]
    m = dm.compute_metrics(ds)
    assert set(m["by_officer"]) == {"cso", "cro"}
    assert m["by_officer"]["cso"]["favorable_rate"] == 1.0
    assert m["by_officer"]["cro"]["favorable_rate"] == 0.0
    assert set(m["by_industry"]) == {"banking", "seguros"}
