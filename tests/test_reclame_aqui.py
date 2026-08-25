import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import reclame_aqui as ra


def _fetcher(routes):
    """Return a fetch(url) that matches by substring against `routes`."""
    def fetch(url):
        for frag, payload in routes.items():
            if frag in url:
                return payload
        return None
    return fetch


SEARCH = {"companies": [
    {"id": 123, "fantasyName": "Nubank", "shortname": "nubank"},
    {"id": 999, "fantasyName": "Nubank Seguros", "shortname": "nubank-seguros"},
]}
REP = {"finalScore": 7.8, "status": "BOM", "complainsCount": 15432,
       "answeredPercentual": 98.5, "solvedPercentual": 82.1, "dealAgainPercentual": 61.0}


def test_search_prefers_exact_name():
    hit = ra._search_company("Nubank", _fetcher({"companySearch": SEARCH}))
    assert hit["id"] == "123" and hit["shortname"] == "nubank"


def test_fetch_reputation_maps_fields():
    fetch = _fetcher({"companySearch": SEARCH, "reputation": REP})
    out = ra.fetch_reputation([{"entity_id": "nubank", "name": "Nubank"}],
                              fetcher=fetch, pause_sec=0, today=dt.date(2026, 8, 25))
    assert len(out) == 1
    s = out[0]
    assert s["entity"] == "nubank" and s["score"] == 7.8 and s["status"] == "BOM"
    assert s["complaints"] == 15432 and s["solved_pct"] == 82.1
    assert s["id"] == "reclameaqui:nubank" and "nubank" in s["url"]


def test_fetch_reputation_degrades_on_missing():
    # search returns nothing -> company skipped, never raises
    out = ra.fetch_reputation([{"entity_id": "x", "name": "Ghost"}],
                              fetcher=_fetcher({}), pause_sec=0)
    assert out == []


def test_summarize_worst_first():
    snaps = [
        {"entity": "a", "score": 8.0, "status": "BOM"},
        {"entity": "b", "score": 3.1, "status": "RUIM"},
    ]
    s = ra.summarize(snaps)
    assert s["total"] == 2 and s["worst"][0]["entity"] == "b"


# --- store ----------------------------------------------------------------
class FakeS3:
    def __init__(self): self.store = {}
    def get_object(self, Bucket, Key):
        if Key not in self.store: raise KeyError(Key)
        return {"Body": _B(self.store[Key])}
    def put_object(self, Bucket, Key, Body, **kw): self.store[Key] = Body
class _B:
    def __init__(self, b): self._b = b
    def read(self): return self._b


def test_merge_keeps_prev_score_for_trend():
    idx = ra.merge_reputation(None, [{"entity": "a", "score": 7.0, "date": "2026-08-01"}],
                              today=dt.date(2026, 8, 25))
    idx2 = ra.merge_reputation(idx, [{"entity": "a", "score": 6.4, "date": "2026-08-25"}],
                               today=dt.date(2026, 8, 25))
    rec = idx2["records"]["a"]
    assert rec["score"] == 6.4 and rec["prev_score"] == 7.0


def test_update_store_persists():
    s3 = FakeS3()
    out = ra.update_store([{"entity": "a", "score": 5.0, "date": "2026-08-25"}], "b",
                          s3=s3, today=dt.date(2026, 8, 25))
    assert out == {"updated": 1, "records": 1}
    assert "a" in json.loads(s3.store[ra.INDEX_KEY])["records"]
