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
    # ets = (0.40*10 + 0.25*10 + 0.20*5) / 0.85
    assert m["ets"] == round((0.40 * 10 + 0.25 * 10 + 0.20 * 5) / 0.85, 1)


def test_per_officer_and_industry_slices():
    ds = [_d("cso", "aprovado", "favoravel", "banking"), _d("cro", "aprovado", "desfavoravel", "seguros")]
    m = dm.compute_metrics(ds)
    assert set(m["by_officer"]) == {"cso", "cro"}
    assert m["by_officer"]["cso"]["favorable_rate"] == 1.0
    assert m["by_officer"]["cro"]["favorable_rate"] == 0.0
    assert set(m["by_industry"]) == {"banking", "seguros"}
