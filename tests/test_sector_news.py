"""#176 (incident #173): per-industry topic registry + sector topic news queries. No network.

The replay fixtures are the live Google News RSS for two of betting's registry queries,
fetched 2026-09-27 (two days after MP 1.394 banned online betting).
"""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import federal_acts, registry, trade_press
from src.synth import entity_registry

_NEWS = Path(__file__).parent / "fixtures" / "news"
_BETS = (_NEWS / "gnews_bets_proibicao_2026-09-27.xml").read_bytes()
_APOSTAS = (_NEWS / "gnews_apostas_regulamentacao_2026-09-27.xml").read_bytes()


# --- the registry -------------------------------------------------------------------------------
def test_topic_registry_covers_real_industries_within_budget():
    slugs = [t.industry for t in registry.INDUSTRY_TOPICS]
    assert len(slugs) == len(set(slugs))
    assert set(slugs) <= set(entity_registry.INDUSTRIES)
    assert {"betting", "banking", "fintech", "insurance", "securitization", "asset-management",
            "crypto", "consorcio", "closed-pension"} <= set(slugs)
    for t in registry.INDUSTRY_TOPICS:
        assert 1 <= len(t.news_queries) <= registry.MAX_NEWS_QUERIES_PER_INDUSTRY, t.industry
        assert t.dou_phrases and t.vocabulary, t.industry
    # every vocabulary compiles, and every DOU phrase is recognised by its own industry's vocabulary
    pats = federal_acts.vocabulary_patterns()
    for t in registry.INDUSTRY_TOPICS:
        for p in t.dou_phrases:
            assert t.industry in federal_acts.industries_in(p), (t.industry, p)


def test_betting_dou_topics_keep_the_pre_175_terms_and_the_env_toggle():
    topics = registry.dou_topic_terms()
    for p in ("Secretaria de Prêmios e Apostas", "apostas de quota fixa", "Lei nº 14.790", "jogos de azar"):
        assert topics[p] == ["betting"]
    assert "apostas" not in topics                       # bare "apostas" measured and rejected
    off = registry.dou_topic_terms(enabled=lambda s: s.industry != "betting")
    assert "apostas de quota fixa" not in off and "instituições de pagamento" in off
    organs = registry.dou_topic_organs()
    assert set(organs) == set(topics)
    assert "Presidência da República$" in organs["jogos de azar"]


def test_lambda_port_has_no_hard_coded_dou_topics_left():
    src = (Path(__file__).resolve().parents[1] / "src" / "ingest" / "lambda_port.py").read_text(encoding="utf-8")
    assert "registry.dou_topic_terms(" in src and '"apostas de quota fixa"' not in src


def test_vocabulary_guards_against_known_homonyms():
    assert federal_acts.industries_in("pagamento do seguro-desemprego") == []
    assert federal_acts.industries_in("Consórcio Interfederativo Minas Gerais: aviso de licitação") == []
    assert "consorcio" in federal_acts.industries_in("Banco Central muda regras dos consórcios")
    assert "insurance" in federal_acts.industries_in("Susep muda regras de seguros")


# --- fetch_sector_news: tagging, precision, union --------------------------------------------------
def _fetcher(pages):
    return lambda q: pages.get(q, b"")


def test_replay_betting_queries_return_the_ban_headlines_tagged_betting():
    queries = {"betting": ["bets proibição", "apostas regulamentação"]}
    items = trade_press.fetch_sector_news(
        queries, lookback_days=7, today=dt.date(2026, 9, 27), pause_sec=0,
        fetcher=_fetcher({"bets proibição": _BETS, "apostas regulamentação": _APOSTAS}))
    titles = [i["title"] for i in items]
    assert any(t.startswith("Presidente Lula assina Medida Provisória que proíbe as bets") for t in titles)
    assert "Bets passam a ser proibidas no país e apostador receberá saldo" in titles
    assert any("Governo Anuncia a Proibição de Bets" in t for t in titles)
    for i in items:
        assert i["query_kind"] == "sector" and i["industries"] == ["betting"]
        assert i["company"] is None and i["name"] is None
        assert i["sector_query"] in queries["betting"]
    # the headline both queries returned is ONE item
    dup = "Da liberação à proibição: Veja a linha do tempo das bets no Brasil"
    assert titles.count(dup) == 1
    assert len(items) >= 20


def _rss(rows):
    body = "".join(f"<item><title>{t}</title><link>{u}</link><pubDate>{d}</pubDate>"
                   f"<source url='x'>{p}</source></item>" for t, u, d, p in rows)
    return f"<?xml version='1.0'?><rss><channel>{body}</channel></rss>".encode()


D = "Fri, 25 Sep 2026 10:00:00 GMT"


def test_sector_headline_must_carry_the_industrys_vocabulary_and_union_industries():
    pages = {
        "q-bet": _rss([("Mega-Sena acumula e apostas podem ser feitas até sábado", "http://a/1", D, "G1"),
                       ("Governo proíbe bets e manda devolver saldo via Pix", "http://a/2", D, "Folha")]),
        "q-pix": _rss([("Governo proíbe bets e manda devolver saldo via Pix", "http://a/2", D, "Folha")]),
    }
    items = trade_press.fetch_sector_news({"betting": ["q-bet"], "fintech": ["q-pix"]},
                                          lookback_days=7, today=dt.date(2026, 9, 27), pause_sec=0,
                                          fetcher=_fetcher(pages))
    assert [i["title"] for i in items] == ["Governo proíbe bets e manda devolver saldo via Pix"]
    assert items[0]["industries"] == ["betting", "fintech"]


def test_sector_queries_cap_per_industry_and_old_items_dropped():
    calls = []

    def fetch(q):
        calls.append(q)
        return _rss([("Bets: governo publica regras", "http://a/1", "Wed, 01 Jan 2026 10:00:00 GMT", "X")])

    items = trade_press.fetch_sector_news({"betting": ["a", "b", "c", "d"]}, lookback_days=7,
                                          today=dt.date(2026, 9, 27), pause_sec=0, fetcher=fetch)
    assert calls == ["a", "b", "c"] and items == []


# --- merge / dedupe against entity items -----------------------------------------------------------
def test_merge_dedupes_by_url_and_title_and_keeps_the_entity_item():
    entity = [{"id": "news:e1", "title": "Bets estão proibidas, mas jogatina continua pelas loterias da Caixa",
               "url": "https://news.google.com/rss/articles/AAA", "company": "Caixa", "name": "Caixa",
               "publisher": "UOL", "date": "2026-09-26", "query_kind": "entity"}]
    sector = [
        {"id": "news:s1", "title": "Bets estão proibidas, mas jogatina continua pelas loterias da Caixa!",
         "url": "https://news.google.com/rss/articles/OTHER", "company": None, "publisher": "UOL",
         "date": "2026-09-26", "query_kind": "sector", "industries": ["betting"]},              # same title
        {"id": "news:s2", "title": "Outra manchete", "url": "https://www.news.google.com/rss/articles/AAA/",
         "company": None, "date": "2026-09-26", "query_kind": "sector", "industries": ["fintech"]},  # same URL
        {"id": "news:s3", "title": "Governo proíbe bets", "url": "https://x/3", "company": None,
         "date": "2026-09-27", "query_kind": "sector", "industries": ["betting"]},
    ]
    out = trade_press.merge_sector_news(entity, sector)
    assert [o["id"] for o in out] == ["news:s3", "news:e1"]
    caixa = out[1]
    assert caixa["company"] == "Caixa" and caixa["query_kind"] == "entity"
    assert caixa["industries"] == ["betting", "fintech"]


def test_fetch_news_runs_sector_queries_outside_the_entity_cap():
    entity_calls = []

    def fetch(term):
        entity_calls.append(term)
        return b""

    out = trade_press.fetch_news(
        ["Nubank", "Inter", "C6"], lookback_days=7, today=dt.date(2026, 9, 27), max_terms=1,
        include_outlets=False, pause_sec=0, fetcher=fetch,
        sector_queries={"betting": ["bets proibição"]}, sector_fetcher=_fetcher({"bets proibição": _BETS}))
    assert entity_calls == ["Nubank"]                                  # entity cap still applies
    assert out and all(o["query_kind"] == "sector" for o in out)       # sector ran regardless


# --- the Lambda news slice --------------------------------------------------------------------------
def test_news_slice_carries_sector_items_with_their_own_budget(monkeypatch):
    from src.ingest import lambda_port

    entity_item = {"id": "news:e1", "title": "Bets estão proibidas, mas jogatina continua pelas loterias da Caixa",
                   "url": "http://u/1", "company": "Caixa", "name": "Caixa", "publisher": "UOL",
                   "date": "2026-09-26", "query_kind": "entity"}
    seen_queries = {}

    def fake_sector(queries, vocab, lookback_days=14, **k):
        seen_queries.update(queries)
        return [{"id": "news:s1", "title": "Governo proíbe bets", "url": "http://u/2", "company": None,
                 "name": None, "publisher": "Folha", "date": "2026-09-26", "query_kind": "sector",
                 "industries": ["betting"]},
                {"id": "news:s2", "title": entity_item["title"], "url": "http://u/9", "company": None,
                 "name": None, "publisher": "UOL", "date": "2026-09-26", "query_kind": "sector",
                 "industries": ["betting"]}]

    budgets = []
    real_budget = lambda_port._source_budget

    def spy_budget(label, deadline, per_source):
        budgets.append(label)
        return real_budget(label, deadline, per_source)

    monkeypatch.setattr(lambda_port, "_source_budget", spy_budget)
    monkeypatch.setattr(lambda_port.trade_press, "fetch_news", lambda terms, **k: [dict(entity_item)])
    monkeypatch.setattr(lambda_port.trade_press, "fetch_sector_news", fake_sector)
    monkeypatch.setattr(lambda_port, "_new_since_last_run",
                        lambda source, docs, seed_if_empty=False, commit=True: docs)
    monkeypatch.setenv("ONCA_NEWS_WATCHLIST", "Caixa")
    monkeypatch.setenv("ONCA_NEWS_USE_REGISTRY", "false")
    monkeypatch.setenv("ONCA_NEWS_MAX_TERMS", "1")

    sl = lambda_port._news_slice(None)
    assert budgets == ["Sector news", "Trade press"]
    assert seen_queries == registry.news_topic_queries()
    assert sl["count"] == 2 and sorted(sl["fetched_ids"]) == ["news:e1", "news:s1"]
    by_id = {i["id"]: i for i in sl["items"]}
    assert by_id["news:s1"]["query_kind"] == "sector" and by_id["news:s1"]["industries"] == ["betting"]
    assert by_id["news:s1"]["company"] is None
    assert by_id["news:e1"]["company"] == "Caixa" and by_id["news:e1"]["industries"] == ["betting"]
    json.dumps(sl)  # the slice stays JSON-serialisable


def test_news_slice_sector_queries_can_be_switched_off(monkeypatch):
    from src.ingest import lambda_port

    def boom(*a, **k):
        raise AssertionError("sector queries must not run")

    monkeypatch.setattr(lambda_port.trade_press, "fetch_news", lambda terms, **k: [])
    monkeypatch.setattr(lambda_port.trade_press, "fetch_sector_news", boom)
    monkeypatch.setenv("ONCA_NEWS_WATCHLIST", "Caixa")
    monkeypatch.setenv("ONCA_NEWS_USE_REGISTRY", "false")
    monkeypatch.setenv("ONCA_NEWS_SECTOR_QUERIES", "false")
    assert lambda_port._news_slice(None)["count"] == 0


def test_sector_items_have_their_own_digest_caps(monkeypatch):
    """Live 2026-09-27: 147 new sector items in one shared cap crowded out new entity items."""
    from src.ingest import lambda_port

    ent = [{"id": f"news:e{i}", "title": f"e{i}", "company": "X", "date": "2026-09-20",
            "query_kind": "entity"} for i in range(5)]
    sec = [{"id": f"news:s{i}", "title": f"s{i}", "company": None, "date": "2026-09-26",
            "query_kind": "sector", "industries": ["betting"]} for i in range(9)]
    monkeypatch.setattr(lambda_port.trade_press, "fetch_news", lambda terms, **k: list(ent))
    monkeypatch.setattr(lambda_port.trade_press, "fetch_sector_news", lambda *a, **k: list(sec))
    monkeypatch.setattr(lambda_port, "_new_since_last_run",
                        lambda source, docs, seed_if_empty=False, commit=True: docs)
    monkeypatch.setenv("ONCA_NEWS_WATCHLIST", "X")
    monkeypatch.setenv("ONCA_NEWS_USE_REGISTRY", "false")
    monkeypatch.setenv("ONCA_NEWS_DIGEST_ITEMS", "5")
    monkeypatch.setenv("ONCA_NEWS_DIGEST_SECTOR_ITEMS", "3")
    sl = lambda_port._news_slice(None)
    kinds = [i["query_kind"] for i in sl["items"]]
    assert kinds.count("entity") == 5 and kinds.count("sector") == 3      # newer sector items don't evict
    assert sl["count"] == 14 and len(sl["fetched_ids"]) == 14              # every id still committed
