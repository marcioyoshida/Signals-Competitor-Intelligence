import datetime as dt

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import gdelt_macro


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class _FakeQueryJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class _FakeBqClient:
    def __init__(self, rows):
        self._rows = rows
        self.queries = []

    def query(self, sql):
        self.queries.append(sql)
        return _FakeQueryJob(self._rows)


def _row(title, publisher="reuters.com", url="https://reuters.com/x"):
    return {
        "DocumentIdentifier": url,
        "SourceCommonName": publisher,
        "Extras": f"<PAGE_TITLE>{title}</PAGE_TITLE>",
    }


def test_fetch_macro_news_parses_rows_into_onca_item_shape():
    rows = [_row("Fed rate hike chances firm as dollar ticks up")]
    client = _FakeBqClient(rows)
    news = gdelt_macro.fetch_macro_news(client, target_date=dt.date(2026, 9, 15))
    assert len(news) == 1
    n = news[0]
    assert n["kind"] == "macro"
    assert n["company"] is None and n["name"] is None
    assert n["_entities"] == []
    assert n["source"] == "News"
    assert n["title"] == "Fed rate hike chances firm as dollar ticks up"
    assert n["url"] == "https://reuters.com/x"
    assert n["date"] == "2026-09-15"
    assert n["id"].startswith("gdelt:")


def test_fetch_macro_news_runs_one_query_against_the_client():
    client = _FakeBqClient([])
    gdelt_macro.fetch_macro_news(client, target_date=dt.date(2026, 9, 15))
    assert len(client.queries) == 1
    assert "gdeltv2.gkg_partitioned" in client.queries[0]
    assert "2026-09-15" in client.queries[0]


def test_parse_rows_drops_items_with_no_page_title():
    rows = [{"DocumentIdentifier": "https://x/1", "SourceCommonName": "reuters.com", "Extras": ""}]
    news = gdelt_macro.parse_rows(rows, target_date=dt.date(2026, 9, 15))
    assert news == []


def test_parse_rows_drops_non_english_titles():
    # Same real case that surfaced live: a Reuters regional-edition headline under the
    # reuters.com domain, in Japanese — unreadable without a translation layer this
    # codebase deliberately doesn't have (see trade_press.py's citation-only posture).
    rows = [_row("マクロコープ：高市首相「国債40兆円」発言")]
    news = gdelt_macro.parse_rows(rows, target_date=dt.date(2026, 9, 15))
    assert news == []


def test_parse_rows_dedups_by_title_and_publisher():
    rows = [_row("Fed holds rates steady"), _row("Fed holds rates steady")]
    news = gdelt_macro.parse_rows(rows, target_date=dt.date(2026, 9, 15))
    assert len(news) == 1


def test_parse_rows_unescapes_html_entities_in_title():
    rows = [_row("Bonds &amp; rates: what&#39;s next for the Fed")]
    news = gdelt_macro.parse_rows(rows, target_date=dt.date(2026, 9, 15))
    assert news[0]["title"] == "Bonds & rates: what's next for the Fed"


def test_parse_rows_skips_rows_with_no_url():
    rows = [{"DocumentIdentifier": "", "SourceCommonName": "reuters.com",
              "Extras": "<PAGE_TITLE>Fed rate hike looms</PAGE_TITLE>"}]
    news = gdelt_macro.parse_rows(rows, target_date=dt.date(2026, 9, 15))
    assert news == []


def test_build_query_includes_tightened_theme_set_not_stockmarket():
    query = gdelt_macro._build_query(dt.date(2026, 9, 15), min_offset=200, min_hits=2)
    assert "ECON_INTEREST_RATES" in query
    assert "EPU_POLICY_FEDERAL_RESERVE" in query
    # ECON_STOCKMARKET was deliberately dropped (too broad — pulls single-stock churn).
    assert "ECON_STOCKMARKET" not in query
