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


# --- confidence tiers (option B) -----------------------------------------
def test_confidence_regulatory_from_cvm():
    ev = {"entity": "x", "kind": "recuperacao_judicial", "label": "RJ", "date": "2026-08-20",
          "title": "X pede RJ", "url": "http://cvm/1", "source": "CVM-FatoRelevante",
          "source_kind": "regulatory", "publisher": "cvm", "evidence_id": "f1"}
    idx = ds.merge_distress(None, [ev], today=dt.date(2026, 8, 25))
    rec = idx["records"]["x#recuperacao_judicial"]
    assert rec["confidence"] == "regulatory"


def test_confidence_reported_then_corroborated():
    today = dt.date(2026, 8, 25)
    e1 = {"entity": "y", "kind": "falencia", "label": "Falência", "date": "2026-08-20",
          "title": "Y falência", "url": "https://a.com/1", "source": "News",
          "source_kind": "news", "publisher": "a.com", "evidence_id": "n1"}
    idx = ds.merge_distress(None, [e1], today=today)
    assert idx["records"]["y#falencia"]["confidence"] == "reported"
    # a SECOND independent publisher upgrades to corroborated
    e2 = dict(e1, url="https://b.com/2", publisher="b.com", evidence_id="n2")
    idx2 = ds.merge_distress(idx, [e2], today=today)
    rec = idx2["records"]["y#falencia"]
    assert rec["confidence"] == "corroborated" and len(rec["sources"]) == 2


def test_source_kind_mapping():
    assert ds.source_kind("CVM-FatoRelevante") == "regulatory"
    assert ds.source_kind("DataJud-CNJ") == "court"
    assert ds.source_kind("News") == "news"


def test_detect_from_fatos_is_regulatory():
    fatos = [{"id": "f1", "source": "CVM-FatoRelevante", "date": "2026-08-20",
              "subject": "Pedido de Recuperação Judicial", "company": "ACME",
              "url": "http://cvm/f1"}]
    evs = ds.detect_distress_events(fatos, resolver=_resolver({"f1": ["acme"]}))
    assert evs[0]["source_kind"] == "regulatory"


def test_update_from_digest_mines_news_and_fatos():
    s3 = FakeS3()
    digest = {
        "news": {"items": [{"id": "n1", "source": "News", "date": "2026-08-24",
                            "title": "Priv pede recuperação judicial", "url": "https://x.com/1"}]},
        "fatos": {"items": [{"id": "f1", "source": "CVM-FatoRelevante", "date": "2026-08-24",
                             "subject": "Pedido de Recuperação Judicial", "url": "http://cvm/f1"}]},
    }
    out = ds.update_from_digest(digest, "b", resolver=_resolver({"n1": ["priv"], "f1": ["listada"]}),
                                s3=s3, today=dt.date(2026, 8, 25))
    assert out["records"] == 2
    saved = json.loads(s3.store[ds.INDEX_KEY])
    assert saved["records"]["listada#recuperacao_judicial"]["confidence"] == "regulatory"
    assert saved["records"]["priv#recuperacao_judicial"]["confidence"] == "reported"


def test_seed_distress_curated():
    s3 = FakeS3()
    out = ds.seed_distress("b", [{"entity": "z", "kind": "falencia", "date": "2026-01-01",
                                  "title": "Z falida", "url": "http://vetted/z"}],
                           s3=s3, today=dt.date(2026, 8, 25))
    assert out["seeded"] == 1
    saved = json.loads(s3.store[ds.INDEX_KEY])
    assert saved["records"]["z#falencia"]["confidence"] == "curated"


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
