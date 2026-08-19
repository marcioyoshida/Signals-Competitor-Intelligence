import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import feature_store as fs


def _narr(entity, run_date, score, *, is_alert=False, lenses=None, label=None):
    return {
        "entity": entity,
        "entity_label": label or entity.title(),
        "run_date": run_date,
        "threat_score": score,
        "is_alert": is_alert,
        "lenses": lenses or ["regulatory"],
    }


def test_build_features_baseline_recency_and_break():
    # itau active on 3 days with a jump on the last; nubank on 2 days.
    narratives = [
        _narr("itau", "2026-08-10", 0.30),
        _narr("itau", "2026-08-12", 0.30),
        _narr("itau", "2026-08-14", 0.90, is_alert=True),  # spike vs 0.30 baseline
        _narr("nubank", "2026-08-05", 0.50),
        _narr("nubank", "2026-08-13", 0.55),
    ]
    feats = fs.build_features(
        narratives, as_of="2026-08-18", industry_map={"itau": ["banking"], "nubank": ["banking", "fintech"]}
    )
    by = {e["entity"]: e for e in feats["entities"]}

    itau = by["itau"]
    assert itau["active_days"] == 3
    assert itau["first_seen"] == "2026-08-10" and itau["last_seen"] == "2026-08-14"
    assert itau["days_since_last"] == 4          # 2026-08-18 - 2026-08-14
    assert itau["score_last"] == 0.9 and itau["score_max"] == 0.9
    assert itau["alerts_total"] == 1
    # baseline excludes the last point (0.30, 0.30 -> std 0) so a jump is a big z
    assert itau["score_z"] > 3
    # cadence: gaps 2,2 days -> mean 2
    assert itau["mean_gap_days"] == 2.0
    assert itau["industries"] == ["banking"]

    nubank = by["nubank"]
    assert nubank["days_since_last"] == 5         # last seen 08-13
    assert set(nubank["industries"]) == {"banking", "fintech"}


def test_cohorts_group_by_industry():
    narratives = [
        _narr("itau", "2026-08-14", 0.4),
        _narr("nubank", "2026-08-14", 0.6),
    ]
    feats = fs.build_features(
        narratives, as_of="2026-08-14",
        industry_map={"itau": ["banking"], "nubank": ["banking", "fintech"]},
    )
    cohorts = feats["cohorts"]
    assert cohorts["banking"]["members"] == 2     # itau + nubank
    assert cohorts["fintech"]["members"] == 1     # nubank only
    assert cohorts["banking"]["score_max"] == 0.6


def test_unclassified_and_empty():
    feats = fs.build_features([_narr("x", "2026-08-14", 0.5)], as_of="2026-08-14")
    assert feats["cohorts"]["_unclassified"]["members"] == 1
    empty = fs.build_features([], as_of="2026-08-14")
    assert empty["entity_count"] == 0 and empty["entities"] == [] and empty["cohorts"] == {}


def test_skips_entityless_and_dateless():
    narratives = [
        _narr("itau", "2026-08-14", 0.4),
        {"entity": None, "run_date": "2026-08-14", "threat_score": 0.9},  # no entity
        {"entity": "ghost", "threat_score": 0.9},                        # no date
    ]
    feats = fs.build_features(narratives, as_of="2026-08-14")
    assert [e["entity"] for e in feats["entities"]] == ["itau"]


def test_publish_and_load_roundtrip():
    store = {}

    class FakeS3:
        def put_object(self, Bucket, Key, Body, **kw):
            store[(Bucket, Key)] = Body

        def get_object(self, Bucket, Key):
            import io
            return {"Body": io.BytesIO(store[(Bucket, Key)])}

    s3 = FakeS3()
    feats = fs.build_features([_narr("itau", "2026-08-14", 0.4)], as_of="2026-08-14")
    uri = fs.publish_features(feats, "onca-digests", s3=s3)
    assert uri.endswith("features/latest.json")
    loaded = fs.load_features("onca-digests", s3=s3)
    assert loaded["entity_count"] == 1 and loaded["entities"][0]["entity"] == "itau"
