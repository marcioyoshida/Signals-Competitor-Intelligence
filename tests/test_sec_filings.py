import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import sec_filings


def test_headers_prefer_env(monkeypatch):
    monkeypatch.setenv("ONCA_SEC_USER_AGENT", "TestBot (contact: test@example.com)")
    h = sec_filings._headers()
    assert "test@example.com" in h["User-Agent"]


def test_html_to_text_strips_tags_and_entities():
    html = """
    <HTML><BODY>
    <P>SECURITIES AND EXCHANGE COMMISSION</P>
    <P>Nu Holdings Ltd.&nbsp;reports <b>TPV growth</b></P>
    <script>evil()</script>
    </BODY></HTML>
    """
    text = sec_filings.html_to_text(html)
    assert "SECURITIES AND EXCHANGE COMMISSION" in text
    assert "Nu Holdings Ltd. reports TPV growth" in text
    assert "evil" not in text
    assert "<" not in text


def test_subject_skips_cover_page_boilerplate():
    text = """
    SECURITIES AND EXCHANGE COMMISSION
    Washington, D.C. 20549
    FORM 6-K
    Form 20-F ( X ) Form 40-F
    Yes No ( X )
    Nubank to Add a Banking License in Brazil through the Acquisition of Banco Porto Real
    São Paulo, July 20, 2026 – Nu Holdings Ltd. announced today that it has entered into a share purchase agreement.
    SIGNATURES
    Pursuant to the requirements of the Securities Exchange Act
    """
    subject = sec_filings._subject_from_text(text)
    assert "Banking License" in subject
    assert "SECURITIES AND EXCHANGE" not in subject
    assert "SIGNATURES" not in subject


def test_fetch_primary_text_extracts_body(monkeypatch):
    html = """
    <DOCUMENT><TYPE>6-K<TEXT>
    <HTML><BODY>
    <P>SECURITIES AND EXCHANGE COMMISSION</P>
    <P>FORM 6-K</P>
    <P>Form 20-F ( X ) Form 40-F</P>
    <P>Yes No ( X )</P>
    <P>Nubank to Add a Banking License in Brazil</P>
    <P>Monthly operational metrics: active customers rose 12 percent.</P>
    </BODY></HTML>
    </TEXT></DOCUMENT>
    """

    class FakeResp:
        status_code = 200
        content = html.encode("utf-8")
        encoding = "utf-8"
        headers = {"Content-Type": "text/html"}

        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        sec_filings.requests, "get", lambda *a, **k: FakeResp()
    )
    monkeypatch.setenv("ONCA_SEC_USER_AGENT", "TestBot (contact: t@e.com)")

    url = "https://www.sec.gov/Archives/edgar/data/1691493/0001/nu.htm"
    got = sec_filings.fetch_primary_text(url)
    assert got["content_error"] is None
    assert got["text"] and "active customers rose 12 percent" in got["text"]
    assert got["subject"] and "Banking License" in got["subject"]
    assert "SECURITIES AND EXCHANGE" not in (got["subject"] or "")
    assert got["content_chars"] > 20


def test_fetch_primary_text_skips_pdf():
    url = "https://www.sec.gov/Archives/edgar/data/1/0001/doc.pdf"
    got = sec_filings.fetch_primary_text(url)
    assert got["content_error"] == "pdf_not_extracted"
    assert got["text"] is None


def test_enrich_with_content_sets_text(monkeypatch):
    monkeypatch.setattr(sec_filings.time, "sleep", lambda *_: None)

    def fake_fetch(url, max_chars=100_000, timeout=60):
        return {
            "text": "Body of filing about payments volume.",
            "subject": "Body of filing about payments volume.",
            "content_type": "text/html",
            "content_chars": 36,
            "content_error": None,
        }

    monkeypatch.setattr(sec_filings, "fetch_primary_text", fake_fetch)
    rows = [
        {
            "id": "sec:1:a",
            "url": "https://www.sec.gov/Archives/edgar/data/1/a.htm",
            "text": None,
        }
    ]
    sec_filings.enrich_with_content(rows)
    assert rows[0]["text"].startswith("Body of filing")
    assert rows[0]["subject"]
    assert rows[0]["content_chars"] == 36


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
                "primaryDocDescription": ["6-K", "20-F", "SC 13G"],
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
    # Metadata only by default — content is enriched separately.
    assert all(r.get("text") is None for r in rows)


def test_empty_tickers_returns_empty():
    assert sec_filings.fetch_filings([]) == []
