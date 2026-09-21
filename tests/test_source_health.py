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


def test_coverage_confidence_empty_is_safe():
    assert sh.coverage_confidence([]) == {"score": None, "n_sources": 0, "n_healthy": 0, "n_attention": 0}


def test_coverage_confidence_all_ok_scores_100():
    rows = [{"band": "ok"}, {"band": "ok"}]
    out = sh.coverage_confidence(rows)
    assert out == {"score": 100, "n_sources": 2, "n_healthy": 2, "n_attention": 0}


def test_coverage_confidence_weights_bands_and_counts_attention():
    # #139: one erroring source among many must pull the score down, not vanish.
    rows = [{"band": "ok"}, {"band": "ok"}, {"band": "ok"}, {"band": "error"}]
    out = sh.coverage_confidence(rows)
    assert out["score"] == round((100 * 3 + 10) / 4)
    assert out["n_sources"] == 4 and out["n_healthy"] == 3 and out["n_attention"] == 1


def test_coverage_confidence_never_leaks_source_names():
    # This is the whole point of #139 — the aggregate must never carry per-source identifiers
    # or last_error text, those stay operator-only.
    rows = [{"source": "BCB Pix", "band": "error", "last_error": "HTTP 500 boom"}]
    out = sh.coverage_confidence(rows)
    assert "source" not in out and "last_error" not in out
    assert set(out.keys()) == {"score", "n_sources", "n_healthy", "n_attention"}


# --- sharded store: the parallel ingest branches must not clobber each other ----------

class FakeS3:
    """Minimal in-memory S3 with the two calls source_health uses."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        import io
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[Key] = Body


def test_parallel_branches_do_not_drop_each_others_sources():
    """`StructuredIngest` and `NewsIngest` are two Lambda invocations running in an
    sfn.Parallel, and merge_and_publish is a read-modify-write. On one shared key the
    later writer silently dropped the earlier one's sources — which is how Trade press
    went 11 days without a persisted run while fetching fine 3x/day."""
    s3 = FakeS3()

    sh.reset()
    sh.record("Trade press", ok=True)
    sh.merge_and_publish("b", s3=s3, shard="news")

    sh.reset()                                    # a *separate* invocation
    sh.record("BCB Pix", ok=True)
    sh.record("CADE antitrust", ok=True)
    sh.merge_and_publish("b", s3=s3, shard="structured")

    rows = {r["source"] for r in sh.as_rows(sh.load_index("b", s3=s3))}
    assert rows == {"Trade press", "BCB Pix", "CADE antitrust"}


def test_branches_write_disjoint_objects():
    s3 = FakeS3()
    sh.reset(); sh.record("Trade press", ok=True)
    assert sh.merge_and_publish("b", s3=s3, shard="news") == "s3://b/source_health/shard-news.json"
    sh.reset(); sh.record("BCB Pix", ok=True)
    sh.merge_and_publish("b", s3=s3, shard="structured")
    assert set(s3.objects) == {"source_health/shard-news.json",
                               "source_health/shard-structured.json"}
    # each shard holds only its own branch
    assert set(sh.load_index("b", s3=s3, shard="news")["records"]) == {"Trade press"}


def test_legacy_single_key_history_is_not_lost_by_the_split():
    s3 = FakeS3()
    import json as _json
    s3.objects["source_health/index.json"] = _json.dumps(
        {"records": {"Old source": {"source": "Old source", "runs": 9,
                                    "last_ok": "2026-09-01T00:00:00+00:00"}}}).encode()
    sh.reset(); sh.record("Trade press", ok=True)
    sh.merge_and_publish("b", s3=s3, shard="news")
    recs = sh.load_index("b", s3=s3)["records"]
    assert set(recs) == {"Old source", "Trade press"}


def test_a_shard_accumulates_its_own_history_across_runs():
    s3 = FakeS3()
    for _ in range(3):
        sh.reset(); sh.record("Trade press", ok=True)
        sh.merge_and_publish("b", s3=s3, shard="news")
    assert sh.load_index("b", s3=s3)["records"]["Trade press"]["runs"] == 3


def test_combine_takes_the_later_record_without_summing_runs():
    """Shards already accumulate; a source seen in two shards must not double-count."""
    out = sh.combine([
        {"records": {"X": {"source": "X", "runs": 5, "last_run": "2026-09-01T00:00:00+00:00",
                           "last_ok": "2026-09-01T00:00:00+00:00"}}},
        {"records": {"X": {"source": "X", "runs": 7, "last_run": "2026-09-10T00:00:00+00:00",
                           "last_error": "boom"}}},
    ])
    assert out["records"]["X"]["runs"] == 7
    # a newer erroring record must not erase the older success
    assert out["records"]["X"]["last_ok"] == "2026-09-01T00:00:00+00:00"


def test_a_new_shard_inherits_history_from_the_pre_split_key():
    """Introducing the shards must not reset every `runs` counter to 1 and lose the
    history accumulated under the old single key."""
    s3 = FakeS3()
    import json as _json
    s3.objects["source_health/index.json"] = _json.dumps({"records": {
        "Trade press": {"source": "Trade press", "runs": 10,
                        "last_ok": "2026-09-09T01:57:53+00:00"},
        "BCB Pix": {"source": "BCB Pix", "runs": 44,
                    "last_ok": "2026-09-20T00:00:00+00:00"},
    }}).encode()

    sh.reset(); sh.record("Trade press", ok=True)
    sh.merge_and_publish("b", s3=s3, shard="news")

    recs = sh.load_index("b", s3=s3)["records"]
    assert recs["Trade press"]["runs"] == 11          # 10 inherited + this run
    # a source this branch did NOT run is left alone in the legacy key
    assert recs["BCB Pix"]["runs"] == 44
    # and the shard only claimed the source it actually ran
    assert set(sh.load_index("b", s3=s3, shard="news")["records"]) == {"Trade press"}


def test_idle_records_a_healthy_run_for_an_event_driven_source():
    """`Receita QSA` and `entities auto-create` fire only when an official register adds
    an institution. Staying silent when nothing happened made them drift to warn/stale
    while working perfectly (1,742 entrants fetched, 0 new, 6 days). Silence from an
    event-driven source is the absence of an EVENT, not of health."""
    sh.reset()
    sh.record("Receita QSA", ok=True, idle=True)
    rec = sh.ledger()["Receita QSA"]
    assert rec["idle"] is True and rec["last_ok"] and rec["last_error"] is None
    assert sh._band(rec) == "ok"


def test_a_source_that_really_ran_is_not_marked_idle():
    sh.reset()
    sh.record("BCB Pix", ok=True, docs=12)
    assert sh.ledger()["BCB Pix"]["idle"] is False
