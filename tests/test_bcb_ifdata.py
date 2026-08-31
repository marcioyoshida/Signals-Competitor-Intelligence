import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import bcb_ifdata


def test_market_share_groups_by_codinst_and_resolves_names():
    # IfDataValores rows carry no institution name field (confirmed against
    # the live API) — only CodInst. Grouping by name instead of CodInst was
    # the bug: every row silently collapsed into one "?" bucket.
    rows = [
        {"CodInst": "00068987", "NomeColuna": "Ativo Total", "Saldo": 300.0},
        {"CodInst": "00068987", "NomeColuna": "Carteira de Crédito", "Saldo": 999.0},
        {"CodInst": "00012345", "NomeColuna": "Ativo Total", "Saldo": 700.0},
    ]
    names = {"00068987": "Banco A", "00012345": "Banco B"}

    result = bcb_ifdata.market_share(rows, institution_names=names)

    assert result == [
        {"institution": "Banco B", "value": 700.0, "share_pct": 70.0},
        {"institution": "Banco A", "value": 300.0, "share_pct": 30.0},
    ]


def test_market_share_falls_back_to_code_when_names_missing():
    rows = [{"CodInst": "00068987", "NomeColuna": "Ativo Total", "Saldo": 100.0}]

    result = bcb_ifdata.market_share(rows)

    assert result == [{"institution": "00068987", "value": 100.0, "share_pct": 100.0}]


# --- name -> entity_id resolution + durable store (ADR 015 §3) -----------

_SHARES = [
    {"institution": "ITAU UNIBANCO", "value": 700.0, "share_pct": 70.0},
    {"institution": "BANCO DO BRASIL", "value": 300.0, "share_pct": 30.0},
    {"institution": "SOME UNTRACKED BANK", "value": 10.0, "share_pct": 5.0},
]


def _resolver(item):
    return {
        "ITAU UNIBANCO": ["itau"],
        "BANCO DO BRASIL": ["bb"],
    }.get(item.get("institution"), [])


def test_map_to_entities_resolves_and_keeps_share():
    recs = bcb_ifdata.map_to_entities(
        _SHARES, resolver=_resolver, base_date=202603, today=dt.date(2026, 8, 31)
    )
    by = {r["entity"]: r for r in recs}
    # unknown institution is dropped (never invented)
    assert set(by) == {"itau", "bb"}
    assert by["itau"]["market_share_pct"] == 70.0
    assert by["itau"]["value"] == 700.0
    assert by["itau"]["base_date"] == 202603
    assert by["itau"]["source"] == "BCB" and by["itau"]["id"] == "bcb-ifdata:itau"
    assert by["bb"]["market_share_pct"] == 30.0


def test_map_to_entities_keeps_largest_share_on_duplicate():
    shares = [
        {"institution": "ITAU A", "value": 1.0, "share_pct": 20.0},
        {"institution": "ITAU B", "value": 2.0, "share_pct": 45.0},
    ]
    recs = bcb_ifdata.map_to_entities(shares, resolver=lambda i: ["itau"])
    assert len(recs) == 1 and recs[0]["market_share_pct"] == 45.0


def test_map_to_entities_skips_null_share():
    shares = [{"institution": "ITAU", "value": None, "share_pct": None}]
    assert bcb_ifdata.map_to_entities(shares, resolver=lambda i: ["itau"]) == []


class FakeS3:
    def __init__(self): self.store = {}
    def get_object(self, Bucket, Key):
        if Key not in self.store: raise KeyError(Key)
        return {"Body": _B(self.store[Key])}
    def put_object(self, Bucket, Key, Body, **kw): self.store[Key] = Body
class _B:
    def __init__(self, b): self._b = b
    def read(self): return self._b


def test_store_roundtrip_and_share_by_entity():
    s3 = FakeS3()
    recs = bcb_ifdata.map_to_entities(
        _SHARES, resolver=_resolver, base_date=202603, today=dt.date(2026, 8, 31)
    )
    out = bcb_ifdata.update_store(recs, "b", s3=s3, today=dt.date(2026, 8, 31))
    assert out == {"updated": 2, "records": 2}
    index = bcb_ifdata.load_index("b", s3=s3)
    # list_records is share-desc
    assert [r["entity"] for r in bcb_ifdata.list_records(index)] == ["itau", "bb"]
    # the projection feed_builder joins on
    assert bcb_ifdata.share_by_entity(index) == {"itau": 70.0, "bb": 30.0}
