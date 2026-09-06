"""ADR 022 Phase 5 — FinBERT financial-tone feature (offline, injected score_fn)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import financial_tone as ft

_SOUNDNESS = {
    "as_of": "2026-09-06", "base_date": 202603,
    "records": {
        "xp": {"indice_basileia": 11.93, "capital_principal": 7.17, "band": "atenção"},
        "btg": {"indice_basileia": 15.91, "capital_principal": 11.36, "band": "sólido"},
        "nodata": {"band": "sólido"},   # no Basileia → no sentences → excluded
    },
}


def _fake_score(text: str) -> dict[str, float]:
    # crude stand-in: 'sólido' reads positive, 'atenção' negative
    if "sólido" in text:
        return {"POSITIVE": 0.9, "NEGATIVE": 0.1, "NEUTRAL": 0.1}
    if "atenção" in text:
        return {"POSITIVE": 0.2, "NEGATIVE": 0.8, "NEUTRAL": 0.1}
    return {"POSITIVE": 0.5, "NEGATIVE": 0.5, "NEUTRAL": 0.2}


def test_sentences_state_the_real_figures():
    s = ft.sentences_for(_SOUNDNESS["records"]["xp"])
    assert any("11.93%" in x and "atenção" in x for x in s)
    assert any("CET1" in x and "7.17%" in x for x in s)
    assert ft.sentences_for({"band": "sólido"}) == []   # no numbers → no sentences


def test_build_tone_is_shadow_and_labelled_inference():
    idx = ft.build_tone(_SOUNDNESS, _fake_score)
    assert idx["shadow"] is True and idx["base_date"] == 202603 and idx["count"] == 2
    assert "nodata" not in idx["records"]
    xp, btg = idx["records"]["xp"], idx["records"]["btg"]
    assert xp["is_inference"] is True and xp["corpus"] == "solvency_facts"
    # sólido reads more positive than atenção
    assert btg["financial_tone_net"] > xp["financial_tone_net"]


def test_net_tone_is_pos_minus_neg_in_range():
    idx = ft.build_tone(_SOUNDNESS, _fake_score)
    for r in idx["records"].values():
        assert -1.0 <= r["financial_tone_net"] <= 1.0


def test_tone_by_entity_projection():
    proj = ft.tone_by_entity(ft.build_tone(_SOUNDNESS, _fake_score))
    assert set(proj) == {"xp", "btg"}
    assert "financial_tone_net" in proj["xp"] and proj["xp"]["band"] == "atenção"
