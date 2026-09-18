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


def test_outlet_matching_does_not_let_a_short_name_swallow_a_longer_word():
    # #135 mode 1: "Invest" is a prefix of "investimentos", a routine word in
    # Brazilian financial press — plain substring containment mis-attributed any
    # such headline to the small FX broker "Invest". Word-boundary matching fixes
    # this without touching the legitimate full-phrase match below it.
    items = [
        ("O que muda nos investimentos no Brasil com a 1ª alta de juros",
         "https://valor.globo.com/n/1", "Thu, 14 Aug 2026 10:00:00 GMT", ""),
        ("Invest amplia base de clientes e lucro no trimestre",
         "https://valor.globo.com/n/2", "Thu, 14 Aug 2026 10:00:00 GMT", ""),
    ]
    news = trade_press.fetch_news(
        ["Invest"], lookback_days=30, today=dt.date(2026, 8, 16),
        fetcher=lambda t: b"<rss><channel></channel></rss>",
        include_outlets=True, outlet_feeds=[("Valor Econômico", "http://feed")],
        outlet_fetcher=lambda url: _rss(items), pause_sec=0,
    )
    assert [n["title"] for n in news] == ["Invest amplia base de clientes e lucro no trimestre"]


def test_google_news_matching_does_not_let_a_short_name_swallow_a_longer_word():
    # Same #135 mode-1 fix applied to the per-term Google News path, not just outlets.
    items = [
        ("O que muda nos investimentos no Brasil com a 1ª alta de juros",
         "http://g1/1", "Thu, 13 Aug 2026 10:00:00 GMT", "G1"),
    ]
    news = trade_press.fetch_news(
        ["Invest"], lookback_days=30, today=dt.date(2026, 8, 16),
        fetcher=lambda t: _rss(items), include_outlets=False, pause_sec=0,
    )
    assert news == []


def test_outlet_matching_homonym_is_a_known_unresolved_limitation():
    # #135 mode 2: two distinct real companies can share a brand ("Centauro" the
    # insurer vs. the sporting-goods retailer). Word-boundary matching does NOT
    # (and can't, by itself) disambiguate this — deliberately left as-is here;
    # the homonym strategy is tracked separately in #135, not silently assumed fixed.
    items = [
        ("Split Payment pode reduzir lucro de Magalu, Pague Menos e Centauro",
         "https://valor.globo.com/n/1", "Thu, 14 Aug 2026 10:00:00 GMT", ""),
    ]
    news = trade_press.fetch_news(
        ["Centauro"], lookback_days=30, today=dt.date(2026, 8, 16),
        fetcher=lambda t: b"<rss><channel></channel></rss>",
        include_outlets=True, outlet_feeds=[("Valor Econômico", "http://feed")],
        outlet_fetcher=lambda url: _rss(items), pause_sec=0,
    )
    assert len(news) == 1  # still matches — homonym disambiguation is future work


def test_iso_accepts_both_rfc822_and_iso8601_pubdates():
    # RSS nominally mandates RFC-822, but Exame emits ISO-8601. Both must
    # normalize to the same YYYY-MM-DD; anything unparseable stays "".
    assert trade_press._iso("Wed, 16 Sep 2026 20:36:44 GMT") == "2026-09-16"
    assert trade_press._iso("2026-09-16T20:36:44") == "2026-09-16"
    assert trade_press._iso("2026-09-16T20:36:44+00:00") == "2026-09-16"
    assert trade_press._iso("") == ""
    assert trade_press._iso(None) == ""
    assert trade_press._iso("not a date") == ""


def test_outlet_feed_with_iso8601_dates_is_not_silently_dropped():
    # Regression: an ISO-8601 pubDate used to fail parsedate_to_datetime, yield
    # date="" from _iso, and get dropped by the cutoff check in fetch_news — so
    # adding Exame's feed would have looked wired while contributing nothing.
    items = [
        ("Nubank amplia lucro no trimestre", "https://exame.com/invest/1", "2026-08-14T09:30:00", ""),
        ("Nubank inaugura mostra de arte", "https://exame.com/pop/2", "2026-08-14T09:30:00", ""),  # no finance context
    ]
    news = trade_press.fetch_news(
        ["Nubank"], lookback_days=30, today=dt.date(2026, 8, 16),
        fetcher=lambda t: b"<rss><channel></channel></rss>",  # no Google News results
        include_outlets=True, outlet_feeds=[("Exame", "http://feed")],
        outlet_fetcher=lambda url: _rss(items), pause_sec=0,
    )
    assert len(news) == 1
    n = news[0]
    assert n["publisher"] == "Exame"
    assert n["date"] == "2026-08-14"                       # parsed, not ""
    assert n["url"] == "https://exame.com/invest/1"        # direct publisher link


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


def test_finance_context_capital_city_sense_does_not_pass():
    # #135 mode-1 class, found INSIDE the finance gate while measuring the live
    # watchlist: bare "capital" fired on the CAPITAL CITY sense, which is routine
    # in BR press. "Maceió vira capital nordestina..." was the single false
    # positive in the whole measured sample — it attributed a tech-event headline
    # to the bank Neon. The financial senses are collocations, so they still pass.
    from src.ingest.trade_press import _has_finance_context
    assert not _has_finance_context("Maceió vira capital nordestina da inovação com o NEON 2026")
    assert not _has_finance_context("Capital paulista recebe evento de inovação com a Stone")
    # the real hits that DID depend on "capital" must survive
    assert _has_finance_context("Azos rompe barreira dos R$ 100 bi em capital segurado")
    assert _has_finance_context("Empresa busca capital de giro para expandir operação")
    assert _has_finance_context("Stone anuncia abertura de capital na Nasdaq")
    assert _has_finance_context("Assembleia aprova aumento de capital social")
    assert _has_finance_context("Fundo de capital semente investe em startups")


def test_finance_context_covers_investir_verbs_but_not_investigar():
    # Measured recall gap: "investiment" (the noun) alone dropped real funding
    # signals like "Nomad investe mais de R$ 100M...". The verb/agent forms are
    # added WITHOUT the bare stem "investi", which would also match
    # "investigação/investigar" — the very word-sense collision #135 is about.
    from src.ingest.trade_press import _has_finance_context
    assert _has_finance_context("Nomad investe mais de R$ 100M para trazer o Time Out Market")
    assert _has_finance_context("Munich Re investe na Azos")
    assert _has_finance_context("Fundo investiu R$ 50 milhões na fintech")
    assert _has_finance_context("Grupo vai investir R$ 2 bilhões em novas lojas")
    assert _has_finance_context("Investidores acompanham a decisão do Copom")
    # investiga* must NOT satisfy the finance gate on its own
    assert not _has_finance_context("Polícia investiga suposto esquema na empresa")
    assert not _has_finance_context("Investigação aponta irregularidades na obra")


def test_news_exclude_vetoes_a_homonym_on_the_google_news_path():
    # #135 mode 2: "NEON 2026" is an innovation event in Alagoas, not Neon the bank.
    # Measured live — it passes the word-boundary AND finance gates ("investimentos"),
    # so only curated per-entity knowledge can separate it from the real brand.
    items = [
        ("NEON 2026 gera R$ 20 milhões em negócios e investimentos",
         "http://x/1", "Thu, 13 Aug 2026 10:00:00 GMT", "X"),
        ("Neon capta R$ 300 milhões em nova rodada",
         "http://x/2", "Thu, 13 Aug 2026 10:00:00 GMT", "X"),
    ]
    common = dict(lookback_days=30, today=dt.date(2026, 8, 16),
                  fetcher=lambda t: _rss(items), include_outlets=False, pause_sec=0)
    both = trade_press.fetch_news(["Neon"], **common)
    assert len(both) == 2  # without the veto the event leaks in

    vetoed = trade_press.fetch_news(["Neon"], excludes={"neon": ["NEON 2026"]}, **common)
    assert [r["title"] for r in vetoed] == ["Neon capta R$ 300 milhões em nova rodada"]


def test_news_exclude_vetoes_a_homonym_on_the_outlet_path_too():
    # The outlet path is where #135 was first observed, so the veto must apply there
    # as well — and must not suppress a DIFFERENT entity that matches the same title.
    items = [
        ("NEON 2026 movimenta o ecossistema de investimentos de Alagoas",
         "https://valor.globo.com/n/1", "Thu, 14 Aug 2026 10:00:00 GMT", ""),
    ]
    news = trade_press.fetch_news(
        ["Neon"], lookback_days=30, today=dt.date(2026, 8, 16),
        excludes={"neon": ["NEON 2026"]},
        fetcher=lambda t: _rss([]), outlet_fetcher=lambda u: _rss(items),
        outlet_feeds=[("Valor", "https://valor.globo.com/feed")], pause_sec=0,
    )
    assert news == []


def test_news_exclude_is_accent_and_case_insensitive_and_optional():
    from src.ingest.trade_press import _excluded, _fold
    assert _excluded("Neon", _fold("NEON 2026 abre inscrições"), {"neon": ["neon 2026"]})
    # an un-normalized map key still vetoes — a silently-skipped veto would ship the
    # wrong attribution, the exact failure this guards against
    assert _excluded("Neon", _fold("cobertura do NEON 2026"), {"NEON": ["NEON 2026"]})
    # accents folded on both sides
    assert _excluded("Pao", _fold("Pão de Açúcar vende ativos"), {"pao": ["pão de açúcar"]})
    # absent / empty config is a no-op, never a crash
    assert not _excluded("Neon", _fold("Neon capta R$ 300 mi"), None)
    assert not _excluded("Neon", _fold("Neon capta R$ 300 mi"), {})
    assert not _excluded("Neon", _fold("Neon capta R$ 300 mi"), {"outra": ["x"]})
