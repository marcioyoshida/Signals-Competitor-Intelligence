"""ESG (B3 ISE membership) proxy — fetcher + registry backfill (issue #30)."""
from __future__ import annotations

from typing import Any

from src.ingest import esg_ise_b3
from src.synth import entity_registry as R


class _FakeTable:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def get_item(self, Key):
        it = self.items.get(Key["pk"])
        return {"Item": it} if it else {}

    def put_item(self, Item):
        self.items[Item["pk"]] = dict(Item)

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}


_B3_RESPONSE = {
    "header": {"date": "31/08/26"},
    "page": {"totalRecords": 2},
    "results": [
        {"cod": "ITUB4", "asset": "ITAUUNIBANCO", "type": "PN", "part": "2,809"},
        {"cod": "PETR4", "asset": "PETROBRAS", "type": "PN", "part": "5,120"},
    ],
}


def test_fetch_portfolio_parses_and_dates():
    pf = esg_ise_b3.fetch_portfolio(fetcher=lambda url: _B3_RESPONSE)
    assert pf["index"] == "ISE B3"
    assert pf["as_of"] == "2026-08-31"       # dd/mm/yy -> ISO
    assert pf["cycle"] == "2026-2027"
    tickers = {c["ticker"]: c for c in pf["constituents"]}
    assert tickers["ITUB4"]["weight_pct"] == 2.809   # comma decimal parsed


def test_fetch_portfolio_raises_on_bad_shape():
    import pytest
    with pytest.raises(ValueError):
        esg_ise_b3.fetch_portfolio(fetcher=lambda url: ["not", "a", "dict"])


def test_match_tracked_entities_uses_ticker_map():
    pf = esg_ise_b3.fetch_portfolio(fetcher=lambda url: _B3_RESPONSE)
    recs = esg_ise_b3.match_tracked_entities(pf, ticker_map={"ITUB4": "itau", "BBAS3": "bb"})
    assert [r["entity"] for r in recs] == ["itau"]   # PETR4 unmatched, BBAS3 absent
    assert recs[0]["ticker"] == "ITUB4" and recs[0]["source"] == "B3-ISE"


def test_backfill_esg_idempotent_and_clears_dropped_member(monkeypatch):
    t = _FakeTable()
    R.put_entity("itau", "Itaú", ["Itaú"], ticker="ITUB4", table=t)
    # only itau/ITUB4 is a tracked B3 ticker we care about here
    monkeypatch.setattr(R, "B3_TICKERS", {"itau": "ITUB4"})
    pf = esg_ise_b3.fetch_portfolio(fetcher=lambda url: _B3_RESPONSE)

    changed = R.backfill_esg_ise_b3(pf, table=t)
    assert changed and changed[0][0] == "itau"
    assert (R.get_entity("itau", table=t).get("esg") or {}).get("ise_b3") is True
    # idempotent: re-run reports no changes (Decimal-safe compare)
    assert R.backfill_esg_ise_b3(pf, table=t) == []

    # next cycle drops ITUB4 → the stale ise_b3 claim is cleared, not left fabricated
    dropped = {"header": {"date": "31/05/27"}, "page": {"totalRecords": 0}, "results": []}
    pf2 = esg_ise_b3.fetch_portfolio(fetcher=lambda url: dropped)
    R.backfill_esg_ise_b3(pf2, table=t)
    assert (R.get_entity("itau", table=t).get("esg") or {}) == {}
