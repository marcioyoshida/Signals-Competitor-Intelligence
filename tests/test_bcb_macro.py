import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import bcb_macro


def test_fetch_selic_detects_last_decision():
    # flat 15.00 then a cut to 14.75 then flat — the decision is the cut.
    rows = (
        [{"data": f"0{i}/07/2026", "valor": "15.00"} for i in range(1, 6)]
        + [{"data": f"0{i}/08/2026", "valor": "14.75"} for i in range(1, 6)]
    )
    out = bcb_macro.fetch_selic(fetcher=lambda url: rows)
    assert out["current"] == 14.75
    assert out["as_of"] == "2026-08-05"
    d = out["last_decision"]
    assert d["previous"] == 15.00 and d["value"] == 14.75
    assert d["direction"] == "baixa" and d["bps"] == -25
    assert d["date"] == "2026-08-01"


def test_fetch_selic_no_change_is_manutencao():
    rows = [{"data": f"0{i}/08/2026", "valor": "14.00"} for i in range(1, 6)]
    out = bcb_macro.fetch_selic(fetcher=lambda url: rows)
    assert out["current"] == 14.00
    assert out["last_decision"]["direction"] == "manutenção" and out["last_decision"]["bps"] == 0


def test_fetch_selic_empty_returns_none():
    assert bcb_macro.fetch_selic(fetcher=lambda url: []) is None


def test_fetch_focus_computes_week_over_week_delta():
    def fake(url, params):
        # newest first; a week-ago snapshot present for the >=5-day rule
        return [
            {"Indicador": "IPCA", "DataReferencia": "2026", "Mediana": 5.05, "Data": "2026-08-14"},
            {"Indicador": "IPCA", "DataReferencia": "2026", "Mediana": 5.02, "Data": "2026-08-13"},
            {"Indicador": "IPCA", "DataReferencia": "2026", "Mediana": 4.90, "Data": "2026-08-07"},
        ]

    out = bcb_macro.fetch_focus(
        indicators=["IPCA"], years=[2026], today=dt.date(2026, 8, 14), fetcher=fake
    )
    assert len(out) == 1
    r = out[0]
    assert r["indicator"] == "IPCA" and r["ref_year"] == 2026
    assert r["median"] == 5.05 and r["prev_median"] == 4.90
    assert r["delta"] == 0.15  # 5.05 - 4.90


def test_fetch_focus_missing_prior_leaves_delta_none():
    def fake(url, params):
        return [{"Indicador": "Selic", "DataReferencia": "2027", "Mediana": 10.5, "Data": "2026-08-14"}]

    out = bcb_macro.fetch_focus(indicators=["Selic"], years=[2027], today=dt.date(2026, 8, 14), fetcher=fake)
    assert out[0]["median"] == 10.5 and out[0]["prev_median"] is None and out[0]["delta"] is None
