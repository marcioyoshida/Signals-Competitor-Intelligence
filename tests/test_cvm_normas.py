"""CVM normative + enforcement ingester (#194, audit R5 fix #7)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import datetime as dt

from src.ingest import cvm_normas as cn
from src.ingest import federal_acts

_LEGIS_FEED = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
<item>
  <pubDate>Fri, 11 Sep 2026 16:30:22 -0300</pubDate>
  <title><![CDATA[Ofício Circular CVM/SSE 03/26, de 11 de Setembro de 2026]]></title>
  <link>http://www.cvm.gov.br/legislacao/oficios-circulares/sse1/oc-sse-0326.html</link>
  <description><![CDATA[
    <p>&nbsp;Impossibilidade de vinculação da taxa de performance de FIDC à remuneração da consultoria.</p>
  ]]></description>
</item>
<item>
  <pubDate>Tue, 15 Sep 2026 18:19:08 -0300</pubDate>
  <title><![CDATA[Resolução CVM 243, de 20 de Maio de 2026]]></title>
  <link>http://www.cvm.gov.br/legislacao/resolucoes/resol243.html</link>
  <description><![CDATA[<p>Altera a Resolução CVM 24.</p><p>(Publicada no DOU de 21.05.2026)</p>]]></description>
</item>
</channel></rss>"""

_NOTICIAS_LISTING = """<html><body><ul>
<li><div class="conteudo">
<div class="subtitulo-noticia">ATIVIDADE SANCIONADORA</div>
<h2 class="titulo"><a href="https://www.gov.br/cvm/pt-br/assuntos/noticias/2026/cvm-aplica-multas-que-somam-mais-de-r-200-milhoes-em-caso-envolvendo-banco-master">CVM aplica multas que somam mais de R$ 200 milhões em caso envolvendo Banco Master</a></h2>
<span class="descricao"><span class="data">16/09/2026</span><span> - </span>Colegiado também julgou outros dois processos em sessão realizada em 8/9/2026</span>
</div></li>
<li><div class="conteudo">
<div class="subtitulo-noticia">MERCADO DE CAPITAIS</div>
<h2 class="titulo"><a href="https://www.gov.br/cvm/pt-br/assuntos/noticias/2026/suspensa-oferta-de-certificados-de-recebiveis-imobiliarios-cri-da-opea-securitizadora-s-a">Suspensa oferta de Certificados de Recebíveis Imobiliários (CRI) da OPEA Securitizadora S.A.</a></h2>
<span class="descricao"><span class="data">03/09/2026</span><span> - </span>A SRE suspendeu a oferta.</span>
</div></li>
<li><div class="conteudo">
<div class="subtitulo-noticia">AGENDA INTERNACIONAL</div>
<h2 class="titulo"><a href="https://www.gov.br/cvm/pt-br/assuntos/noticias/2026/presidente-participa-de-forum">Presidente da CVM participa de fórum em Nova York</a></h2>
<span class="descricao"><span class="data">17/09/2026</span><span> - </span>Agenda de viagem.</span>
</div></li>
</ul></body></html>"""

_ARTICLE_PAGE = """<html><body>
<div id="parent-fieldname-text" class="">
  <div property="rnews:articleBody"><p>A CVM realizou sessão de julgamento do PAS 19957.007976/2020-94: Banco Master S/A - Em Liquidação Extrajudicial e outros. Multas totalizando R$ 203 milhões.</p></div>
</div>
<div id="viewlet-below-content">footer</div>
</body></html>"""

_TODAY = dt.date(2026, 9, 27)


def _fetcher(pages):
    def get(url):
        return pages.get(url, "")
    return get


def test_fetch_legislacao_parses_cdata_and_dou_date():
    recs = cn.fetch_legislacao(
        lookback_days=150,  # wide enough to include the Resolução's 2026-05-21 DOU date
        fetcher=_fetcher({cn.LEGISLACAO_FEED_URL: _LEGIS_FEED}),
        today=_TODAY,
    )
    assert len(recs) == 2
    oc = next(r for r in recs if "SSE" in r["title"])
    assert oc["id"] == "cvm-legis:oc-sse-0326.html"
    assert oc["kind"] == "regulatory" and oc["source"] == "CVM"
    assert oc["organ"] == "Comissão de Valores Mobiliários"
    assert oc["doc_type"].startswith("Ofício")
    assert oc["date"] == "2026-09-11"
    assert "performance de FIDC" in oc["text"]

    res = next(r for r in recs if "Resolução" in r["title"])
    assert res["date"] == "2026-05-21"  # from "(Publicada no DOU de 21.05.2026)", not pubDate


def test_fetch_legislacao_respects_lookback_cutoff():
    recs = cn.fetch_legislacao(
        lookback_days=5,  # cutoff excludes both items (2026-09-11 / 2026-05-21)
        fetcher=_fetcher({cn.LEGISLACAO_FEED_URL: _LEGIS_FEED}),
        today=_TODAY,
    )
    assert recs == []


def test_fetch_noticias_filters_to_enforcement_and_fetches_full_text():
    pages = {
        cn.NOTICIAS_LISTING_URL: _NOTICIAS_LISTING,
        "https://www.gov.br/cvm/pt-br/assuntos/noticias/2026/cvm-aplica-multas-que-somam-mais-de-r-200-milhoes-em-caso-envolvendo-banco-master": _ARTICLE_PAGE,
    }
    recs = cn.fetch_noticias(lookback_days=60, fetcher=_fetcher(pages), today=_TODAY)
    # the travel/agenda item is dropped; the sanction + stop-order items are kept
    assert len(recs) == 2
    titles = {r["title"] for r in recs}
    assert any("Banco Master" in t for t in titles)
    assert any("OPEA" in t for t in titles)

    master = next(r for r in recs if "Banco Master" in r["title"])
    assert master["id"].startswith("cvm-noticia:")
    assert master["kind"] == "regulatory" and master["organ"] == "Comissão de Valores Mobiliários"
    assert master["doc_type"] == "PAS (julgamento)"
    assert master["date"] == "2026-09-16"
    # full article text was fetched (quotable), not just the listing's one-line summary
    assert "R$ 203 milhões" in master["text"]

    opea = next(r for r in recs if "OPEA" in r["title"])
    assert opea["doc_type"] == "Suspensão de oferta (SRE)"
    # no detail page stubbed for OPEA -> falls back to the listing summary, never fabricated
    assert opea["text"] == "A SRE suspendeu a oferta."


def test_fetch_noticias_drops_irrelevant_agenda_items():
    pages = {cn.NOTICIAS_LISTING_URL: _NOTICIAS_LISTING}
    recs = cn.fetch_noticias(
        lookback_days=60, fetcher=_fetcher(pages), fetch_full_text=False, today=_TODAY
    )
    assert not any("Nova York" in r["title"] for r in recs)


def test_fetch_recent_dedupes_and_sorts_desc():
    pages = {
        cn.LEGISLACAO_FEED_URL: _LEGIS_FEED,
        cn.NOTICIAS_LISTING_URL: _NOTICIAS_LISTING,
    }
    recs = cn.fetch_recent(
        lookback_days=60, fetcher=_fetcher(pages), fetch_full_text=False, today=_TODAY
    )
    ids = [r["id"] for r in recs]
    assert len(ids) == len(set(ids))
    dates = [r.get("date") or "" for r in recs]
    assert dates == sorted(dates, reverse=True)


def test_degrades_to_empty_on_fetch_failure():
    def boom(_url):
        raise RuntimeError("network down")

    assert cn.fetch_legislacao(fetcher=boom) == []
    assert cn.fetch_noticias(fetcher=boom) == []
    assert cn.fetch_recent(fetcher=boom) == []


def test_annotate_classifies_the_opea_stop_order_as_critical_securitization():
    # This is the audit's B3 finding, end to end: ingest -> classify.
    pages = {
        cn.NOTICIAS_LISTING_URL: _NOTICIAS_LISTING,
        "https://www.gov.br/cvm/pt-br/assuntos/noticias/2026/suspensa-oferta-de-certificados-de-recebiveis-imobiliarios-cri-da-opea-securitizadora-s-a":
            "<html><body><div id=\"parent-fieldname-text\"><div>A SRE suspendeu a oferta "
            "pública de distribuição de Certificados de Recebíveis Imobiliários (CRI) da "
            "OPEA Securitizadora S.A. por irregularidades na documentação.</div></div>"
            "<div id=\"viewlet-below\">x</div></body></html>",
    }
    recs = cn.fetch_noticias(lookback_days=60, fetcher=_fetcher(pages), today=_TODAY)
    annotated = federal_acts.annotate(recs)
    opea = next(r for r in annotated if "OPEA" in r["title"])
    assert opea["severity"] == "critical"
    assert "securitization" in (opea.get("industries") or [])
