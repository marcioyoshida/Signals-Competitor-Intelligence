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


# --- #308 per-day bundles ----------------------------------------------------------------------
import hashlib  # noqa: E402

AS_OF = dt.date(2026, 11, 10)


def _d(n):
    return (AS_OF - dt.timedelta(days=n)).isoformat()


class _BundleS3:
    """LIST returns content ETags; counts GETs per kind; put_object stores bundles."""

    def __init__(self, objs, *, writable=True):
        self.objs = {k: json.dumps(v) for k, v in objs.items()}
        self.writable = writable
        self.gets = []

    def put(self, key, value):
        self.objs[key] = json.dumps(value)

    def get_paginator(self, _name):
        fake = self

        class _P:
            def paginate(self, Bucket, Prefix):
                keys = sorted(k for k in fake.objs if k.startswith(Prefix))
                yield {"Contents": [{"Key": k, "ETag": '"%s"' % hashlib.md5(fake.objs[k].encode()).hexdigest()}
                                    for k in keys]}
        return _P()

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        if Key not in self.objs:
            raise KeyError(Key)
        if Key.endswith("bad.json"):
            raise RuntimeError("unreadable")
        return {"Body": io.BytesIO(self.objs[Key].encode())}

    def put_object(self, Bucket, Key, Body, **kw):
        if not self.writable:
            raise PermissionError("AccessDenied")
        self.objs[Key] = Body.decode()

    def narrative_gets(self):
        return [k for k in self.gets if k.startswith("narratives/")]


def _objs():
    return {f"narratives/{_d(0)}/t.json": {"id": "today"},
            f"narratives/{_d(1)}/a.json": {"id": "y-a"},
            f"narratives/{_d(1)}/b.json": {"id": "y-b"},
            f"narratives/{_d(3)}/c.json": {"id": "old-c"}}


def test_first_read_bundles_past_days_then_reads_bundles():
    s3 = _BundleS3(_objs())
    first = feature_store.load_history("b", 7, s3=s3, as_of=AS_OF)
    assert [o["id"] for o in first] == ["old-c", "y-a", "y-b", "today"]
    assert sorted(k for k in s3.objs if k.startswith("narrative_bundles/")) == \
        [f"narrative_bundles/{_d(3)}.json", f"narrative_bundles/{_d(1)}.json"]  # never today
    s3.gets.clear()
    second = feature_store.load_history("b", 7, s3=s3, as_of=AS_OF)
    assert second == first
    assert s3.narrative_gets() == [f"narratives/{_d(0)}/t.json"]  # only today per object


def test_rewritten_added_or_deleted_narrative_rebuilds_that_day():
    s3 = _BundleS3(_objs())
    feature_store.load_history("b", 7, s3=s3, as_of=AS_OF)
    s3.put(f"narratives/{_d(1)}/a.json", {"id": "y-a", "edited": True})   # same key, new content
    s3.gets.clear()
    out = feature_store.load_history("b", 7, s3=s3, as_of=AS_OF)
    assert {"id": "y-a", "edited": True} in out
    assert sorted(s3.narrative_gets()) == sorted([f"narratives/{_d(1)}/a.json", f"narratives/{_d(1)}/b.json",
                                                  f"narratives/{_d(0)}/t.json"])
    s3.put(f"narratives/{_d(3)}/new.json", {"id": "old-new"})              # added
    del s3.objs[f"narratives/{_d(1)}/b.json"]                               # deleted / quarantined
    out = [o["id"] for o in feature_store.load_history("b", 7, s3=s3, as_of=AS_OF)]
    assert out == ["old-c", "old-new", "y-a", "today"]
    s3.gets.clear()
    assert [o["id"] for o in feature_store.load_history("b", 7, s3=s3, as_of=AS_OF)] == out
    assert s3.narrative_gets() == [f"narratives/{_d(0)}/t.json"]


def test_unreadable_object_is_never_frozen_into_a_bundle():
    objs = _objs()
    objs[f"narratives/{_d(1)}/bad.json"] = {"id": "bad"}
    s3 = _BundleS3(objs)
    out = [o["id"] for o in feature_store.load_history("b", 7, s3=s3, as_of=AS_OF)]
    assert "bad" not in out and "y-a" in out
    assert f"narrative_bundles/{_d(1)}.json" not in s3.objs
    assert f"narrative_bundles/{_d(3)}.json" in s3.objs


def test_read_only_caller_still_loads_and_kill_switch(monkeypatch):
    s3 = _BundleS3(_objs(), writable=False)
    assert [o["id"] for o in feature_store.load_history("b", 7, s3=s3, as_of=AS_OF)] == \
        ["old-c", "y-a", "y-b", "today"]
    s3 = _BundleS3(_objs())
    monkeypatch.setattr(feature_store, "_BUNDLES", False)
    feature_store.load_history("b", 7, s3=s3, as_of=AS_OF)
    assert not [k for k in s3.objs if k.startswith("narrative_bundles/")]


def test_stale_bundle_with_wrong_item_count_is_ignored():
    s3 = _BundleS3(_objs())
    feature_store.load_history("b", 7, s3=s3, as_of=AS_OF)
    key = f"narrative_bundles/{_d(1)}.json"
    b = json.loads(s3.objs[key]); b["items"] = b["items"][:1]; s3.objs[key] = json.dumps(b)
    assert [o["id"] for o in feature_store.load_history("b", 7, s3=s3, as_of=AS_OF)] == \
        ["old-c", "y-a", "y-b", "today"]


def test_read_only_caller_warns_once(capsys):
    s3 = _BundleS3(_objs(), writable=False)
    feature_store.load_history("b", 7, s3=s3, as_of=AS_OF)
    assert capsys.readouterr().out.count("narrative bundles not written") == 1
