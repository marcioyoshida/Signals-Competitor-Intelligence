import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import bcb_reclamacoes as bc


def test_to_float_br_format():
    assert bc._to_float("84,90") == 84.9
    assert bc._to_float("1.263.435,45") == 1263435.45
    assert bc._to_float("") is None


ROWS = {
    "RankingMaioresBancos": {"value": [
        {"Posicao": 1, "Ano": 2026, "Periodo": 2, "TipoPeriodo": "T",
         "Categoria": "Top 15", "InstituicaoFinanceira": "BRADESCO", "Indice": "84,90"},
        {"Posicao": 3, "Ano": 2026, "Periodo": 2, "TipoPeriodo": "T",
         "Categoria": "Top 15", "InstituicaoFinanceira": "ITAU", "Indice": "46,87"},
        # an older quarter that must be filtered out
        {"Posicao": 1, "Ano": 2025, "Periodo": 4, "TipoPeriodo": "T",
         "Categoria": "Top 15", "InstituicaoFinanceira": "BRADESCO", "Indice": "90,00"},
    ]},
    "RankingDemaisBancos": {"value": []},
}


def _fetch(url):
    for res, payload in ROWS.items():
        if res in url:
            return payload
    return None


def test_fetch_ranking_keeps_latest_quarter():
    rows = bc.fetch_ranking(fetcher=_fetch)
    assert len(rows) == 2  # 2026-T2 only; the 2025-T4 row dropped
    assert all(r["Ano"] == 2026 for r in rows)


def test_map_to_entities_resolves_and_normalizes():
    rows = bc.fetch_ranking(fetcher=_fetch)
    resolver = lambda item: {"BRADESCO": ["bradesco"], "ITAU": ["itau"]}.get(item.get("title"), [])
    recs = bc.map_to_entities(rows, resolver=resolver, today=dt.date(2026, 8, 25))
    by = {r["entity"]: r for r in recs}
    assert by["bradesco"]["rank"] == 1 and by["bradesco"]["index"] == 84.9
    assert by["bradesco"]["period"] == "2026-T2" and by["bradesco"]["source"] == "BCB"
    assert by["itau"]["rank"] == 3


def test_map_keeps_best_rank_on_duplicate():
    rows = [
        {"Posicao": 9, "Ano": 2026, "Periodo": 2, "InstituicaoFinanceira": "X", "Indice": "1,0"},
        {"Posicao": 4, "Ano": 2026, "Periodo": 2, "InstituicaoFinanceira": "X", "Indice": "2,0"},
    ]
    recs = bc.map_to_entities(rows, resolver=lambda i: ["x"])
    assert len(recs) == 1 and recs[0]["rank"] == 4


def test_summarize_worst_first():
    recs = [{"entity": "a", "rank": 5, "index": 1, "period": "2026-T2"},
            {"entity": "b", "rank": 1, "index": 9, "period": "2026-T2"}]
    s = bc.summarize(recs)
    assert s["worst"][0]["entity"] == "b" and s["period"] == "2026-T2"


class FakeS3:
    def __init__(self): self.store = {}
    def get_object(self, Bucket, Key):
        if Key not in self.store: raise KeyError(Key)
        return {"Body": _B(self.store[Key])}
    def put_object(self, Bucket, Key, Body, **kw): self.store[Key] = Body
class _B:
    def __init__(self, b): self._b = b
    def read(self): return self._b


def test_store_roundtrip():
    s3 = FakeS3()
    out = bc.update_store([{"entity": "a", "rank": 2, "index": 3.0, "period": "2026-T2"}],
                          "b", s3=s3, today=dt.date(2026, 8, 25))
    assert out == {"updated": 1, "records": 1}
    recs = bc.list_records(bc.load_index("b", s3=s3))
    assert recs[0]["entity"] == "a"
