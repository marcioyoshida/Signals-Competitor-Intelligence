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


def test_outlet_matching_covers_terms_beyond_google_cap():
    # Registry-scale regression: a term past max_terms is NOT queried on Google
    # News (expensive per-term loop is capped), but the cheap outlet-feed path
    # must still match the FULL term set — else late-alphabet entities lose all
    # news coverage once the registry grows past the cap.
    google_hits = {"queried": []}

    def _google(term):
        google_hits["queried"].append(term)
        return b"<rss><channel></channel></rss>"  # no per-term results

    outlet_items = [("Ripio expande exchange de cripto no Brasil", "https://lc/1", "Thu, 14 Aug 2026 10:00:00 GMT", "")]
    news = trade_press.fetch_news(
        ["Alpha", "Beta", "Ripio"], lookback_days=30, today=dt.date(2026, 8, 16),
        max_terms=2,  # only Alpha, Beta hit Google; Ripio is past the cap
        fetcher=_google,
        include_outlets=True, outlet_feeds=[("Livecoins", "http://feed")],
        outlet_fetcher=lambda url: _rss(outlet_items), pause_sec=0,
    )
    assert google_hits["queried"] == ["Alpha", "Beta"]      # Ripio never queried on Google
    assert [n["company"] for n in news] == ["Ripio"]        # but resolved via the outlet
    assert news[0]["publisher"] == "Livecoins"


def test_finance_context_word_start_not_mid_word():
    # deeper-fix regression: "ação" (share) must not match inside "celebração"
    from src.ingest.trade_press import _has_finance_context
    assert not _has_finance_context("Blue Note recebe Rolling Stone Sessions e celebração dos 60 anos")
    assert not _has_finance_context("informação sobre a situação da educação")
    # real finance words / stems still match at a word start
    assert _has_finance_context("StoneCo divulga lucro e receita do trimestre")
    assert _has_finance_context("Nubank amplia pagamentos e crédito")  # stems pagament/credito
    assert _has_finance_context("ação da empresa sobe na bolsa")       # standalone ação


def test_finance_context_crypto_and_consorcio_sectors():
    # crypto / consórcio headlines carry no banking token — the sector cue itself
    # must satisfy the finance gate, else the new modules' news is silently dropped.
    from src.ingest.trade_press import _has_finance_context
    assert _has_finance_context("Mercado Bitcoin amplia oferta de criptoativos")   # cripto stem
    assert _has_finance_context("Binance lança nova exchange no Brasil")           # exchange
    assert _has_finance_context("Ademicon lidera vendas de consórcio no trimestre")  # consorci stem
    assert _has_finance_context("Embracon registra recorde de cartas contempladas")  # contemplad
    # and a plain culture headline still does not
    assert not _has_finance_context("banda faz show de rock no fim de semana")


def test_finance_context_betting_sector():
    from src.ingest.trade_press import _has_finance_context
    assert _has_finance_context("Betano fecha maior patrocínio de apostas do Brasil")   # aposta stem
    assert _has_finance_context("Superbet amplia operação de cassino online")           # cassino
    assert _has_finance_context("SPA autoriza nova casa de apostas de quota fixa")       # aposta
    assert _has_finance_context("bet365 registra alta no GGR do trimestre")             # ggr
    assert not _has_finance_context("time anuncia novo uniforme para a temporada")


def test_finance_context_fund_sector():
    from src.ingest.trade_press import _has_finance_context
    assert _has_finance_context("MXRF11 anuncia rendimento mensal de R$ 0,10 por cota")   # rendiment/cota
    assert _has_finance_context("HGLG11 amplia portfólio imobiliário logístico")          # imobili
    assert _has_finance_context("KNCA11 é o maior FIAGRO de crédito do agro")             # fiagro
    assert _has_finance_context("Fundo imobiliário eleva aluguéis e dividendos")          # imobili/alugu
    assert not _has_finance_context("prefeitura inaugura praça no centro da cidade")


def test_finance_context_acquiring_sector():
    from src.ingest.trade_press import _has_finance_context
    assert _has_finance_context("Cielo perde participação no mercado de adquirência")     # adquir stem
    assert _has_finance_context("Getnet lança nova maquininha para lojistas")             # maquininha
    assert _has_finance_context("Rede reduz MDR para pequenos comerciantes")              # mdr
    assert _has_finance_context("Credenciadora eleva TPV no trimestre")                   # credenciad/tpv
    assert not _has_finance_context("rede de apoio comunitário abre inscrições")
