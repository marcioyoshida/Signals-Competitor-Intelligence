import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import digest_io


class _FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kwargs):
        self.puts.append(kwargs)


def _patch_s3(monkeypatch):
    fake = _FakeS3()
    monkeypatch.setattr(digest_io.boto3, "client", lambda name: fake)
    return fake


def test_write_narrative_partitions_by_run_date(monkeypatch):
    fake = _patch_s3(monkeypatch)
    # data date lags (as_of 08-13) but the run happened 08-17 → key by run date.
    key = digest_io.write_narrative(
        {"id": "cand-ent-itau", "as_of": "2026-08-13", "run_date": "2026-08-17"},
        bucket="b",
    )
    assert key == "narratives/2026-08-17/cand-ent-itau.json"
    assert fake.puts[0]["Key"] == key


def test_write_narrative_falls_back_to_as_of_for_legacy(monkeypatch):
    _patch_s3(monkeypatch)
    key = digest_io.write_narrative({"id": "x", "as_of": "2026-08-13"}, bucket="b")
    assert key == "narratives/2026-08-13/x.json"


import datetime as _dt
import io as _io
import json as _json


class _MergeS3:
    """Minimal S3 stub for load_latest_digest_from_s3 merge tests."""

    def __init__(self, objects):
        # objects: {key: (last_modified, body_dict)}
        self._objs = objects

    def list_objects_v2(self, Bucket, Prefix):
        return {
            "Contents": [
                {"Key": k, "LastModified": lm}
                for k, (lm, _body) in self._objs.items()
                if k.startswith(Prefix)
            ]
        }

    def get_object(self, Bucket, Key):
        body = _json.dumps(self._objs[Key][1]).encode("utf-8")
        return {"Body": _io.BytesIO(body)}


def test_load_latest_overlays_news_slice_onto_structured_base(monkeypatch):
    t = _dt.datetime(2026, 8, 19, 21, 0, 0)
    objs = {
        # structured base digest (news slice empty), older
        "lambda-digests/aaa.json": (
            t, {"regulatory": {"count": 5}, "news": {"count": 0, "items": []}, "source": "lambda_port"},
        ),
        # news slice written by the parallel branch, newer
        "lambda-digests/news/bbb.json": (
            t + _dt.timedelta(seconds=5),
            {"news": {"count": 9, "items": [{"id": "news:1", "company": "Binance"}]}, "source": "news_ingest"},
        ),
    }
    monkeypatch.setattr(digest_io.boto3, "client", lambda name: _MergeS3(objs))
    digest = digest_io.load_latest_digest_from_s3(bucket="b")
    assert digest["regulatory"]["count"] == 5          # base preserved
    assert digest["news"]["count"] == 9                # news overlaid from the slice
    assert digest["news"]["items"][0]["company"] == "Binance"


def test_load_latest_picks_newest_base_and_ignores_news_only(monkeypatch):
    t = _dt.datetime(2026, 8, 19, 20, 0, 0)
    objs = {
        "lambda-digests/old.json": (t, {"regulatory": {"count": 1}, "source": "lambda_port"}),
        "lambda-digests/new.json": (t + _dt.timedelta(minutes=1), {"regulatory": {"count": 2}, "source": "lambda_port"}),
    }
    monkeypatch.setattr(digest_io.boto3, "client", lambda name: _MergeS3(objs))
    digest = digest_io.load_latest_digest_from_s3(bucket="b")
    assert digest["regulatory"]["count"] == 2          # newest base; no news slice -> base kept
