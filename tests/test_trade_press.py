import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import trade_press


def _rss(items):
    body = "".join(
        f"<item><title>{t}</title><link>{l}</link>"
        f"<pubDate>{d}</pubDate><source url='x'>{s}</source></item>"
        for (t, l, d, s) in items
    )
    return (f"<?xml version='1.0'?><rss><channel>{body}</channel></rss>").encode()


def test_filters_title_relevance_finance_and_date():
    items = [
        ("Nubank tem lucro líquido acima de US$ 1 bilhão", "http://g1/1", "Thu, 13 Aug 2026 10:00:00 GMT", "G1"),
        ("Queens Of The Stone Age revelam nova data", "http://bb/2", "Fri, 14 Aug 2026 10:00:00 GMT", "Billboard"),  # off-topic (no finance, no term match for Nubank query)
        ("Nubank Arte Lab inaugura exposição", "http://veja/3", "Wed, 12 Aug 2026 10:00:00 GMT", "Veja"),          # term ok but no finance context
        ("Nubank cresce no crédito e amplia lucro", "http://old/4", "Wed, 01 Jan 2026 10:00:00 GMT", "X"),          # too old
    ]
    news = trade_press.fetch_news(
        ["Nubank"], lookback_days=30, today=dt.date(2026, 8, 16),
        fetcher=lambda t: _rss(items), include_outlets=False, pause_sec=0,
    )
    titles = [n["title"] for n in news]
    assert titles == ["Nubank tem lucro líquido acima de US$ 1 bilhão"]
    n = news[0]
    assert n["source"] == "News" and n["publisher"] == "G1"
    assert n["date"] == "2026-08-13"
    assert n["company"] == "Nubank"
    assert n["url"] == "http://g1/1"


def test_dedup_across_terms_by_headline():
    item = [("Stone registra lucro no trimestre", "http://a/1?utm=x", "Thu, 13 Aug 2026 10:00:00 GMT", "CNN")]
    news = trade_press.fetch_news(
        ["Stone", "StoneCo"], lookback_days=30, today=dt.date(2026, 8, 16),
        fetcher=lambda t: _rss(item), include_outlets=False, pause_sec=0,
    )
    assert len(news) == 1  # same headline/publisher -> one id


def test_finance_filter_can_be_disabled():
    items = [("Nubank Parque recebe show", "http://p/1", "Thu, 13 Aug 2026 10:00:00 GMT", "P")]
    news = trade_press.fetch_news(
        ["Nubank"], lookback_days=30, today=dt.date(2026, 8, 16),
        require_finance_context=False, fetcher=lambda t: _rss(items), include_outlets=False, pause_sec=0,
    )
    assert len(news) == 1


def test_direct_outlet_feed_matches_full_phrase_and_sets_publisher():
    # Valor-style feed: only headlines naming a competitor (full phrase) are kept.
    items = [
        ("Ações do Nubank sobem com lucro recorde", "https://valor.globo.com/n/1", "Thu, 14 Aug 2026 10:00:00 GMT", ""),
        ("Justiça nega indenização e obriga bancos a rever", "https://valor.globo.com/n/2", "Thu, 14 Aug 2026 10:00:00 GMT", ""),  # 'banco' generic, no brand phrase
    ]
    news = trade_press.fetch_news(
        ["Nubank", "Banco Inter"], lookback_days=30, today=dt.date(2026, 8, 16),
        fetcher=lambda t: b"<rss><channel></channel></rss>",  # no Google News results
        include_outlets=True, outlet_feeds=[("Valor Econômico", "http://feed")],
        outlet_fetcher=lambda url: _rss(items), pause_sec=0,
    )
    assert len(news) == 1
    n = news[0]
    assert n["publisher"] == "Valor Econômico"
    assert n["company"] == "Nubank"                    # matched the full brand phrase
    assert n["url"] == "https://valor.globo.com/n/1"    # direct publisher link
