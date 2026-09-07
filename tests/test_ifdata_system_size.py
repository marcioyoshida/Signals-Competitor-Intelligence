"""#75 / R3 — IF.data system-wide market size (SFN asset base) + leaders."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import bcb_ifdata


def _index():
    # itau holds 30% with value 300 → system total = 1000; bb 20%/200; xp 5%/50
    return {"records": {
        "itau": {"entity": "itau", "metric": "Ativo Total", "value": 300.0,
                 "market_share_pct": 30.0, "base_date": 202606},
        "bb": {"entity": "bb", "value": 200.0, "market_share_pct": 20.0, "metric": "Ativo Total"},
        "xp": {"entity": "xp", "value": 50.0, "market_share_pct": 5.0, "metric": "Ativo Total"},
    }}


def test_system_size_derives_total_from_value_and_share():
    m = bcb_ifdata.system_size(_index())
    assert m["size_value"] == 1000.0  # 300 / 0.30
    assert m["metric"] == "Ativo Total" and m["base_date"] == 202606
    assert m["resolved"] == 3
    assert m["top"][0]["entity"] == "itau" and m["top"][0]["share_pct"] == 30.0  # leader first
    assert "sistema" in m["scope"]  # honestly labelled whole-system, not per-sector


def test_system_size_empty_when_no_value_or_share():
    assert bcb_ifdata.system_size({"records": {}}) == {}
    assert bcb_ifdata.system_size({"records": {"x": {"entity": "x", "value": None,
                                                     "market_share_pct": None}}}) == {}


def test_system_size_survives_zero_share_row():
    idx = {"records": {"z": {"entity": "z", "value": 0.0, "market_share_pct": 0.0},
                       "itau": {"entity": "itau", "value": 300.0, "market_share_pct": 30.0}}}
    assert bcb_ifdata.system_size(idx)["size_value"] == 1000.0  # skips the 0/0 row
