"""Multi-bank Pilar 3 — Basel KM1 (LCR/NSFR/Basileia) via DASFN (offline)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import bcb_km1 as km

_GROUPED = {  # Santander-style: rows nested inside groups
    "km1_trimestreReferencia": "2026-2",
    "km1_capitalRegulamentarRwa": {"km1_7": {"t": "0.153", "t_1": "0.152"}},
    "km1_razaoAlavancagem": {"km1_14": {"t": "0.070"}},
    "km1_lcr": {"km1_17": {"t": "1.858", "t_1": "1.891"}},
    "km1_nsfr": {"km1_20": {"t": "1.184"}},
}
_FLAT = {  # Itaú-style: rows at top level
    "km1_trimestreReferencia": "2026-2",
    "km1_7": {"t": "0.154"}, "km1_17": {"t": "2.02"}, "km1_20": {"t": "1.221"},
}
_BAD = {"km1_7": {"t": "14.443"}, "km1_17": {"t": "0.0"}}  # wrong scale → implausible


def test_extract_handles_grouped_and_flat_layouts():
    g = km.extract_km1(_GROUPED)
    assert g["basileia_pct"] == 15.3 and g["lcr_pct"] == 185.8 and g["nsfr_pct"] == 118.4
    assert g["lcr_qoq_pp"] == round(185.8 - 189.1, 1)          # t vs t_1
    f = km.extract_km1(_FLAT)
    assert f["basileia_pct"] == 15.4 and f["lcr_pct"] == 202.0  # flat layout found


def test_plausibility_guard_drops_bad_parse():
    b = km.extract_km1(_BAD)
    assert b["basileia_pct"] is None      # 1444% → implausible, dropped
    assert b["lcr_pct"] == 0.0            # 0 is within bounds (kept)


def test_list_wrapped_doc():
    assert km.extract_km1([_FLAT])["lcr_pct"] == 202.0


def test_lcr_band():
    assert km.lcr_band(95) == "crítica" and km.lcr_band(120) == "atenção"
    assert km.lcr_band(185) == "confortável" and km.lcr_band(None) is None


def test_base_date_parses_ref_quarter_and_filing():
    assert km._base_date(".../km1/26-08-29_060653_20260630_km1.json") == 20260630
    assert km._base_date("https://cda.itau.com.br/dadosabertos/pilar3/km1/2026-2") == 20260630
    assert km._base_date("no-date-here") == 0


def test_km1_by_entity_projection():
    recs = [{"entity": "santander", "lcr_pct": 185.8, "nsfr_pct": 118.4, "band_lcr": "confortável",
             "basileia_pct": 15.3}]
    proj = km.km1_by_entity(km.merge(None, recs))
    assert proj["santander"]["lcr_pct"] == 185.8 and proj["santander"]["band_lcr"] == "confortável"
