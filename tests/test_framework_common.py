import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import framework_common as fc
from src.synth.framework_common import fz


DIM = {
    "axis_bound": (fz("comparative"), fz()),
    "lens_bound": (fz(), fz("entrants", "news")),
    "both": (fz("regulatory"), fz("dou")),
    "unconstrained": (fz(), fz()),
}


def _ev(id, axis=None, lenses=None):
    return {"id": id, "axis": axis, "lenses": lenses or []}


def test_on_signal_axis_and_lens_matching():
    assert fc.on_signal(_ev("a", axis="comparative"), DIM["axis_bound"])
    assert not fc.on_signal(_ev("a", axis="news"), DIM["axis_bound"])
    assert fc.on_signal(_ev("a", lenses=["news"]), DIM["lens_bound"])
    assert not fc.on_signal(_ev("a", lenses=["market"]), DIM["lens_bound"])
    # both: either side satisfies
    assert fc.on_signal(_ev("a", axis="regulatory"), DIM["both"])
    assert fc.on_signal(_ev("a", lenses=["dou"]), DIM["both"])
    # unconstrained always matches
    assert fc.on_signal(_ev("a"), DIM["unconstrained"])


def test_on_signal_ids_filters_and_dedups():
    evidence = [
        _ev("e0", axis="comparative"),      # on for axis_bound
        _ev("e1", lenses=["entrants"]),      # off for axis_bound
        _ev("e0", axis="comparative"),      # dup id
    ]
    ids = fc.on_signal_ids([0, 1, 2], evidence, "axis_bound", DIM)
    assert ids == ["e0"]  # e1 dropped (off-signal), e0 deduped


def test_on_signal_ids_unknown_dimension_is_unconstrained():
    evidence = [_ev("e0", axis="whatever", lenses=["zzz"])]
    assert fc.on_signal_ids([0], evidence, "not_in_map", DIM) == ["e0"]


def test_on_signal_ids_ignores_out_of_range_and_bad_indices():
    evidence = [_ev("e0", axis="comparative")]
    assert fc.on_signal_ids([5, "x", 0], evidence, "axis_bound", DIM) == ["e0"]


def test_kill_switch_keeps_off_signal(monkeypatch):
    monkeypatch.setenv("ONCA_FRAMEWORK_SIGNAL_GATE", "0")
    evidence = [_ev("e0", axis="news")]  # off-signal for axis_bound
    assert fc.on_signal_ids([0], evidence, "axis_bound", DIM) == ["e0"]
