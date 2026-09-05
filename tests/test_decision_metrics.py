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


def test_composite_and_tdr_stay_none_no_fabrication():
    m = dm.compute_metrics([_d("cso", "aprovado", "favoravel")])
    assert m["ets"] is None and "parcial" in m["ets_note"]
    assert m["tdr"] is None and "baseline" in m["tdr_note"]


def test_per_officer_and_industry_slices():
    ds = [_d("cso", "aprovado", "favoravel", "banking"), _d("cro", "aprovado", "desfavoravel", "seguros")]
    m = dm.compute_metrics(ds)
    assert set(m["by_officer"]) == {"cso", "cro"}
    assert m["by_officer"]["cso"]["favorable_rate"] == 1.0
    assert m["by_officer"]["cro"]["favorable_rate"] == 0.0
    assert set(m["by_industry"]) == {"banking", "seguros"}
