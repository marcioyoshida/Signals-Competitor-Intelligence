"""ADR 019 completion items C1–C5 (#303), on top of the byte-identical golden digest
(tests/test_adr019_golden.py)."""
from __future__ import annotations

import json

from src.ingest import registry
from tests.test_adr019_golden import _run


def _calls(out, name):
    return [json.loads(c) for c in out["calls"] if json.loads(c)[0] == name]


# --- C1: moves + market in the loop ---------------------------------------------------------------
def test_c1_every_moves_and_snapshot_source_is_declared():
    for sid in ("pix_moves", "juros_moves", "inf_diario_moves", "fiagro_moves"):
        spec = registry.by_id(sid)
        assert spec.delta == "moves" and spec.tracked_key and spec.label
    for sid in ("pix_moves", "juros_moves", "inf_diario_moves"):
        assert registry.by_id(sid).key_field and registry.by_id(sid).value_field
    assert registry.by_id("market").delta == "none"


def test_c1_a_disabled_moves_source_fetches_nothing_and_keeps_its_section(monkeypatch):
    out = _run(monkeypatch, {"ONCA_FIAGRO_SIGNAL": "false"})
    assert _calls(out, "fiagro.fetch") == []
    fia = json.loads(out["digest"])["fiagro_moves"]
    assert fia == {"funds_tracked": 0, "pl_move_count": 0, "cotista_move_count": 0,
                   "new_registration_count": 0, "move_count": 0, "items": [], "context": []}


def test_c1_an_empty_watchlist_touches_no_state(monkeypatch):
    out = _run(monkeypatch, {"ONCA_COMPETITORS": "", "ONCA_INF_DIARIO_USE_COMPETITORS": "false"})
    assert _calls(out, "infd.fetch") == []
    assert not any("cvm_inf_diario" in s for s in out["state"])
    assert json.loads(out["digest"])["inf_diario_moves"]["as_of"] is None
