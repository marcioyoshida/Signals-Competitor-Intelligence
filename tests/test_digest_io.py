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
