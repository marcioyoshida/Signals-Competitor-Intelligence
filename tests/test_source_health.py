"""#76 / R5 — real per-ingester run telemetry (reliability)."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import source_health as sh


def setup_function(_):
    sh.reset()


def test_record_tracks_ok_and_error():
    sh.record("BCB Pix", ok=True)
    sh.record("SUSEP", ok=False, error="HTTP 500 boom")
    led = sh.ledger()
    assert led["BCB Pix"]["last_ok"] and led["BCB Pix"]["last_error"] is None
    assert led["SUSEP"]["last_error"].startswith("HTTP 500") and "last_ok" not in led["SUSEP"]


def test_merge_keeps_prior_last_ok_when_run_errors():
    stored = {"records": {"BCB Pix": {"source": "BCB Pix", "runs": 3,
                                      "last_ok": "2026-09-01T00:00:00+00:00"}}}
    sh.record("BCB Pix", ok=False, error="timeout")  # this run failed
    merged = sh.merge(stored, sh.ledger())
    rec = merged["records"]["BCB Pix"]
    assert rec["last_ok"] == "2026-09-01T00:00:00+00:00"  # prior success preserved
    assert rec["last_error"] == "timeout" and rec["runs"] == 4  # runs accumulate


def test_merge_carries_forward_untouched_sources():
    stored = {"records": {"Idle": {"source": "Idle", "runs": 1,
                                   "last_ok": "2026-08-01T00:00:00+00:00"}}}
    sh.record("Active", ok=True)
    merged = sh.merge(stored, sh.ledger())
    assert set(merged["records"]) == {"Idle", "Active"}  # a source that didn't run persists


def test_as_rows_bands_and_orders_worst_first():
    now = dt.datetime(2026, 9, 7, tzinfo=dt.timezone.utc)
    index = {"records": {
        "fresh": {"source": "fresh", "last_ok": "2026-09-06T00:00:00+00:00"},
        "stale": {"source": "stale", "last_ok": "2026-08-01T00:00:00+00:00"},
        "broken": {"source": "broken", "last_ok": "2026-09-06T00:00:00+00:00", "last_error": "boom"},
        "never": {"source": "never"},
    }}
    rows = sh.as_rows(index, now=now)
    bands = [r["band"] for r in rows]
    assert bands[0] in ("error", "never_ok")  # worst first
    assert {r["source"]: r["band"] for r in rows} == {
        "fresh": "ok", "stale": "stale", "broken": "error", "never": "never_ok"}
