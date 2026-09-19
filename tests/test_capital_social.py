"""#143 — capital social: parse, materiality, and the move projection."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import capital_social as cs


def test_parse_capital_accepts_every_shape_brasilapi_returns():
    assert cs.parse_capital(1000000) == 1000000.0
    assert cs.parse_capital(1000000.50) == 1000000.50
    assert cs.parse_capital("1000000.00") == 1000000.0
    assert cs.parse_capital("1.234.567,89") == 1234567.89   # BR decimal comma
    assert cs.parse_capital("R$ 500.000,00") == 500000.0
    assert cs.parse_capital("2500,75") == 2500.75


def test_parse_capital_keeps_zero_but_rejects_junk():
    # Zero is a REAL registered capital, not a missing value — it must survive.
    assert cs.parse_capital(0) == 0.0
    assert cs.parse_capital("0,00") == 0.0
    assert cs.parse_capital(None) is None
    assert cs.parse_capital("") is None
    assert cs.parse_capital("n/d") is None
    # bool is an int subclass in Python; it is never a capital figure.
    assert cs.parse_capital(True) is None


def test_materiality_needs_both_floors():
    # 50% but only R$250k — a small entrant's housekeeping bump, not news.
    assert cs.is_material(500_000, 750_000) is False
    # R$5m but only 1% — a big bank's rounding, not news.
    assert cs.is_material(500_000_000, 505_000_000) is False
    # Both floors cleared.
    assert cs.is_material(10_000_000, 30_000_000) is True


def test_a_first_observation_is_never_material():
    # There is no prior value to compare, so seeing an entity's capital for the first
    # time must not read as "they just raised capital".
    assert cs.is_material(None, 900_000_000) is False


def test_a_move_off_zero_capital_rests_on_the_absolute_floor():
    # delta_pct cannot be expressed against zero; the move is still real.
    assert cs.delta_pct(0, 5_000_000) is None
    assert cs.is_material(0, 5_000_000) is True
    assert cs.is_material(0, 100_000) is False   # below the absolute floor


def test_direction_and_delta_pct():
    assert cs.direction(10_000_000, 30_000_000) == "aumento"
    assert cs.direction(30_000_000, 10_000_000) == "reducao"
    assert cs.direction(10, 10) is None
    assert cs.delta_pct(10_000_000, 30_000_000) == 200.0
    assert cs.delta_pct(30_000_000, 15_000_000) == -50.0


def _attrs(value, previous, changed_at, label="Banco X"):
    return {"label": label,
            "capital": {"value": value, "previous": previous, "changed_at": changed_at}}


def test_move_record_carries_both_figures_never_a_bare_delta():
    rec = cs.move_record("banco_x", _attrs(30_000_000, 10_000_000, "2026-09-10T00:00:00+00:00"))
    assert rec["capital"] == 30_000_000 and rec["previous_capital"] == 10_000_000
    assert rec["delta"] == 20_000_000 and rec["delta_pct"] == 200.0
    assert rec["direction"] == "aumento" and rec["date"] == "2026-09-10"
    assert rec["kind"] == "capital_social_move"
    assert "Receita" in rec["source"]


def test_an_entity_whose_capital_we_merely_know_is_not_a_move():
    # Cold first observation: a value, no previous. Knowing a number is not a signal.
    assert cs.move_record("banco_x", _attrs(900_000_000, None, None)) is None
    assert cs.move_record("banco_x", {"label": "Banco X"}) is None
    assert cs.move_record("banco_x", {}) is None


def test_moves_from_attrs_drops_stale_moves_and_sorts_newest_first():
    attrs = {
        "old": _attrs(30_000_000, 10_000_000, "2026-01-01T00:00:00+00:00", "Antigo"),
        "new": _attrs(50_000_000, 20_000_000, "2026-09-15T00:00:00+00:00", "Novo"),
        "mid": _attrs(80_000_000, 40_000_000, "2026-08-01T00:00:00+00:00", "Meio"),
        "noise": _attrs(510_000, 500_000, "2026-09-18T00:00:00+00:00", "Ruído"),
    }
    out = cs.moves_from_attrs(attrs, today=dt.date(2026, 9, 19))
    # "old" is outside the 90d window; "noise" clears neither floor.
    assert [r["entity"] for r in out] == ["new", "mid"]


def test_an_undated_move_is_kept_rather_than_silently_lost():
    # A move persisted before dating existed is still a real change; dropping it on a
    # missing date would quietly lose it. It is kept and sorts last.
    attrs = {"dated": _attrs(50_000_000, 20_000_000, "2026-09-15T00:00:00+00:00"),
             "undated": _attrs(90_000_000, 30_000_000, None)}
    out = cs.moves_from_attrs(attrs, today=dt.date(2026, 9, 19))
    assert [r["entity"] for r in out] == ["dated", "undated"]
    assert out[-1]["date"] is None


def test_moves_from_attrs_is_empty_and_safe_on_no_data():
    assert cs.moves_from_attrs(None) == []
    assert cs.moves_from_attrs({}) == []
