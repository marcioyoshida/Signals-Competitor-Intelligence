import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import macro_correlate as mc


def _macro(selic_date=None, gdelt_dates=None):
    macro = {}
    if selic_date:
        macro["selic"] = {"current": 13.75, "last_decision": {"date": selic_date}}
    if gdelt_dates:
        macro["gdelt_macro"] = [{"title": f"Fed headline {i}", "date": d} for i, d in enumerate(gdelt_dates)]
    return macro


def test_macro_event_dates_collects_selic_only():
    # Fixed 2026-09-18: gdelt_macro dates are deliberately EXCLUDED — see module
    # docstring. Its "date" is structurally always "today" (a daily snapshot, not
    # a rare event), so including it flagged ~90% of the live feed on day one.
    events = mc.macro_event_dates(_macro(selic_date="2026-09-17", gdelt_dates=["2026-09-15"]))
    assert set(events.keys()) == {"2026-09-17"}
    assert events["2026-09-17"]["kind"] == "selic"


def test_macro_event_dates_ignores_gdelt_macro_even_without_selic():
    events = mc.macro_event_dates(_macro(gdelt_dates=["2026-09-15", "2026-09-16"]))
    assert events == {}


def test_macro_event_dates_empty_when_no_macro():
    assert mc.macro_event_dates(None) == {}
    assert mc.macro_event_dates({}) == {}


def test_annotate_attaches_note_within_window():
    items = [{"id": "n1", "entity": "itau", "date": "2026-09-16", "narrative": "Itaú repricing"}]
    out = mc.annotate_feed_items(items, _macro(selic_date="2026-09-15"))
    assert out[0]["macro_note"]["kind"] == "selic"
    assert out[0]["macro_note"]["gap_days"] == 1  # item is 1 day AFTER the event
    assert "1 dia(s) depois" in out[0]["macro_note"]["text"]
    assert out[0]["macro_note"]["text"].startswith("inferência:")
    # original item fields preserved
    assert out[0]["entity"] == "itau"
    assert out[0]["narrative"] == "Itaú repricing"


def test_annotate_no_note_outside_window():
    items = [{"id": "n1", "entity": "itau", "date": "2026-09-01"}]
    out = mc.annotate_feed_items(items, _macro(selic_date="2026-09-15"), window_days=3)
    assert "macro_note" not in out[0]


def test_annotate_no_note_without_date():
    items = [{"id": "n1", "entity": "itau"}]
    out = mc.annotate_feed_items(items, _macro(selic_date="2026-09-15"))
    assert "macro_note" not in out[0]


def test_annotate_is_safe_with_no_macro():
    items = [{"id": "n1", "entity": "itau", "date": "2026-09-15"}]
    out = mc.annotate_feed_items(items, None)
    assert out == items  # unchanged, no crash


def test_nearest_picks_closest_when_multiple_events_in_window():
    # macro_event_dates() only ever surfaces at most one Selic date today, so this
    # exercises _nearest()'s tie-breaking directly with a synthetic multi-event
    # dict — still real coverage of the "closest wins" logic, just not reachable
    # through the current single-anchor macro_event_dates() output.
    import datetime as dt
    events = {
        "2026-09-14": {"kind": "selic", "label": "far"},
        "2026-09-17": {"kind": "selic", "label": "near"},
    }
    hit = mc._nearest(dt.date(2026, 9, 16), events, window_days=3)
    assert hit[1] == "2026-09-17"  # 1 day away, vs. 2 days for the other


def test_annotate_never_causal_language():
    items = [{"id": "n1", "date": "2026-09-15"}]
    out = mc.annotate_feed_items(items, _macro(selic_date="2026-09-15"))
    text = out[0]["macro_note"]["text"]
    assert "causou" not in text and "por causa" not in text
    assert "coincide" in text


def test_annotate_ignores_malformed_dates():
    items = [{"id": "n1", "date": "not-a-date"}]
    out = mc.annotate_feed_items(items, _macro(selic_date="2026-09-15"))
    assert "macro_note" not in out[0]


def test_annotate_no_note_from_gdelt_macro_snapshot_alone():
    # Regression for the live bug found 2026-09-18: gdelt_macro's date is always
    # "today" (a daily snapshot), so a narrative dated today must NOT get a note
    # just because gdelt_macro also happens to be dated today — that flagged 406
    # of ~450 live feed items on the first real run.
    items = [{"id": "n1", "date": "2026-09-18"}]
    out = mc.annotate_feed_items(items, _macro(gdelt_dates=["2026-09-18"]))
    assert "macro_note" not in out[0]
