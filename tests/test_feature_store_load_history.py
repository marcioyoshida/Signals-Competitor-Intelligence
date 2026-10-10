"""load_history: lists only the window's date prefixes, fetches in parallel, keeps key order."""
import datetime as dt
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import feature_store  # noqa: E402


class _FakeS3:
    def __init__(self, objs):
        self.objs = objs          # key -> dict
        self.prefixes = []

    def get_paginator(self, _name):
        fake = self

        class _P:
            def paginate(self, Bucket, Prefix):
                fake.prefixes.append(Prefix)
                keys = sorted(k for k in fake.objs if k.startswith(Prefix))
                yield {"Contents": [{"Key": k} for k in keys]}
        return _P()

    def get_object(self, Bucket, Key):
        if Key.endswith("bad.json"):
            raise RuntimeError("unreadable")
        return {"Body": io.BytesIO(json.dumps(self.objs[Key]).encode())}


def test_load_history_window_order_and_skips():
    today = dt.date.today()
    d = lambda n: (today - dt.timedelta(days=n)).isoformat()  # noqa: E731
    objs = {
        f"narratives/{d(0)}/b.json": {"id": "today-b"},
        f"narratives/{d(0)}/a.json": {"id": "today-a"},
        f"narratives/{d(2)}/x.json": {"id": "two-days"},
        f"narratives/{d(2)}/bad.json": {"id": "unreadable"},
        f"narratives/{d(2)}/note.txt": {"id": "not-json"},
        f"narratives/{d(40)}/old.json": {"id": "outside-window"},
    }
    s3 = _FakeS3(objs)
    out = feature_store.load_history("b", 7, s3=s3)
    assert [o["id"] for o in out] == ["two-days", "today-a", "today-b"]  # key order, window only
    assert len(s3.prefixes) == 7 and all(p.startswith("narratives/") for p in s3.prefixes)
