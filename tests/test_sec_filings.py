import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import sec_filings


def test_headers_prefer_env(monkeypatch):
    monkeypatch.setenv("ONCA_SEC_USER_AGENT", "TestBot (contact: test@example.com)")
    h = sec_filings._headers()
    assert "test@example.com" in h["User-Agent"]


def test_fetch_filings_maps_recent_and_filters(monkeypatch):
    tickers_payload = {
        "0": {"ticker": "STNE", "cik_str": 1745431, "title": "StoneCo"},
        "1": {"ticker": "OTHER", "cik_str": 1, "title": "Other"},
    }
    submissions = {
        "name": "StoneCo Ltd.",
        "filings": {
            "recent": {
                "form": ["6-K", "20-F", "SC 13G"],
                "filingDate": ["2026-06-01", "2026-03-01", "2026-05-01"],
                "accessionNumber": ["0001-26-000001", "0001-25-000002", "0001-26-000003"],
                "primaryDocument": ["a.htm", "b.htm", "c.htm"],
            }
        },
    }

    calls = []

    class FakeResp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    def fake_get(url, headers=None, timeout=60):
        calls.append(url)
        if "company_tickers" in url:
            return FakeResp(tickers_payload)
        return FakeResp(submissions)

    monkeypatch.setattr(sec_filings.requests, "get", fake_get)
    monkeypatch.setattr(sec_filings.time, "sleep", lambda *_: None)
    monkeypatch.setenv("ONCA_SEC_USER_AGENT", "TestBot (contact: t@e.com)")

    rows = sec_filings.fetch_filings(
        ["STNE"], lookback_days=400, max_per_ticker=10
    )
    # SC 13G filtered out; 6-K and 20-F kept if within lookback
    forms = {r["form"] for r in rows}
    assert "6-K" in forms
    assert "20-F" in forms
    assert "SC 13G" not in forms
    assert all(r["ticker"] == "STNE" for r in rows)
    assert all(r["url"].startswith("https://www.sec.gov/") for r in rows)
    assert any("1745431" in r["id"] for r in rows)


def test_empty_tickers_returns_empty():
    assert sec_filings.fetch_filings([]) == []
