import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import distress as ds


# --- classify -------------------------------------------------------------
def test_classify_rj():
    assert ds.classify_distress("Varejista X pede recuperação judicial") == "recuperacao_judicial"
    assert ds.classify_distress("Empresa entra em recuperação judicial") == "recuperacao_judicial"


def test_classify_falencia_and_extrajudicial():
    assert ds.classify_distress("Justiça decreta falência da Y") == "falencia"
    assert ds.classify_distress("Z negocia recuperação extrajudicial") == "recuperacao_extrajudicial"


def test_classify_none_for_unrelated():
    assert ds.classify_distress("Banco lança novo cartão de crédito") is None
    assert ds.classify_distress("") is None


# --- detect ---------------------------------------------------------------
def _resolver(mapping):
    return lambda item: mapping.get(item.get("id"), [])


def test_detect_requires_both_distress_and_entity():
    items = [
        {"id": "a", "title": "Loja ABC pede recuperação judicial", "date": "2026-08-20",
         "url": "http://x/a", "source": "News"},
        {"id": "b", "title": "Loja ABC pede recuperação judicial", "date": "2026-08-20"},  # unresolved
        {"id": "c", "title": "Banco lança cartão", "date": "2026-08-20"},                  # not distress
    ]
    evs = ds.detect_distress_events(items, resolver=_resolver({"a": ["lojas_abc"], "c": ["banco_x"]}))
    assert [e["entity"] for e in evs] == ["lojas_abc"]
    assert evs[0]["kind"] == "recuperacao_judicial" and evs[0]["url"] == "http://x/a"


def test_detect_multi_entity_headline():
    items = [{"id": "a", "title": "Grupo pede recuperação judicial", "date": "2026-08-20"}]
    evs = ds.detect_distress_events(items, resolver=_resolver({"a": ["e1", "e2"]}))
    assert {e["entity"] for e in evs} == {"e1", "e2"}


# --- merge / upsert -------------------------------------------------------
def test_merge_creates_and_updates_record():
    today = dt.date(2026, 8, 25)
    e1 = {"entity": "abc", "kind": "recuperacao_judicial", "label": "Recuperação Judicial",
          "date": "2026-08-10", "title": "ABC pede RJ", "url": "u1", "evidence_id": "n1"}
    idx = ds.merge_distress(None, [e1], today=today)
    rec = idx["records"]["abc#recuperacao_judicial"]
    assert rec["first_seen"] == "2026-08-10" and rec["last_seen"] == "2026-08-10"
    assert rec["mentions"] == 1 and idx["count"] == 1

    # a newer mention advances last_seen + latest_title, keeps first_seen
    e2 = dict(e1, date="2026-08-22", title="ABC segue em RJ", url="u2", evidence_id="n2")
    idx2 = ds.merge_distress(idx, [e2], today=today)
    rec2 = idx2["records"]["abc#recuperacao_judicial"]
    assert rec2["first_seen"] == "2026-08-10" and rec2["last_seen"] == "2026-08-22"
    assert rec2["latest_title"] == "ABC segue em RJ" and rec2["mentions"] == 2
    assert "n2" in rec2["evidence"]


def test_merge_prunes_stale():
    today = dt.date(2026, 8, 25)
    old = {"entity": "old", "kind": "falencia", "label": "Falência",
           "date": "2023-01-01", "title": "t", "url": None, "evidence_id": None}
    idx = ds.merge_distress(None, [old], today=today, ttl_days=720)
    assert idx["count"] == 0  # 2+ years old -> pruned


def test_escalation_tracked_separately():
    today = dt.date(2026, 8, 25)
    rj = {"entity": "z", "kind": "recuperacao_judicial", "label": "Recuperação Judicial",
          "date": "2026-06-01", "title": "z RJ", "url": None, "evidence_id": None}
    fal = {"entity": "z", "kind": "falencia", "label": "Falência",
           "date": "2026-08-01", "title": "z falência", "url": None, "evidence_id": None}
    idx = ds.merge_distress(None, [rj, fal], today=today)
    assert idx["count"] == 2
    # entity_status returns the most severe (falência)
    assert ds.entity_status(idx, "z")["kind"] == "falencia"


def test_list_records_sorted_recent_first():
    today = dt.date(2026, 8, 25)
    a = {"entity": "a", "kind": "falencia", "label": "F", "date": "2026-08-01",
         "title": "a", "url": None, "evidence_id": None}
    b = {"entity": "b", "kind": "falencia", "label": "F", "date": "2026-08-20",
         "title": "b", "url": None, "evidence_id": None}
    idx = ds.merge_distress(None, [a, b], today=today)
    recs = ds.list_records(idx)
    assert [r["entity"] for r in recs] == ["b", "a"]


# --- orchestrator (fake S3) -----------------------------------------------
class FakeS3:
    def __init__(self):
        self.store = {}

    def get_object(self, Bucket, Key):
        if Key not in self.store:
            raise KeyError(Key)
        return {"Body": _Body(self.store[Key])}

    def put_object(self, Bucket, Key, Body, **kw):
        self.store[Key] = Body


class _Body:
    def __init__(self, b): self._b = b
    def read(self): return self._b


def test_update_from_news_persists_and_accumulates():
    s3 = FakeS3()
    items = [{"id": "n1", "title": "XPTO pede recuperação judicial", "date": "2026-08-24"}]
    out = ds.update_from_news(items, "bucket", resolver=_resolver({"n1": ["xpto"]}),
                              s3=s3, today=dt.date(2026, 8, 25))
    assert out == {"new_events": 1, "records": 1}
    saved = json.loads(s3.store[ds.INDEX_KEY])
    assert "xpto#recuperacao_judicial" in saved["records"]

    # a second run with a new entity accumulates (durable store)
    items2 = [{"id": "n2", "title": "YZ decreta falência", "date": "2026-08-25"}]
    out2 = ds.update_from_news(items2, "bucket", resolver=_resolver({"n2": ["yz"]}),
                               s3=s3, today=dt.date(2026, 8, 25))
    assert out2["records"] == 2
