"""One display name per news outlet, learned from the data (10-04: 55 outlets had two spellings)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import outlets


def test_key_collapses_name_and_domain_but_not_a_shared_prefix():
    assert outlets.outlet_key("O GLOBO") == outlets.outlet_key("oglobo.globo.com") == "oglobo"
    assert outlets.outlet_key("Estadão") == outlets.outlet_key("www.estadao.com.br") == "estadao"
    assert outlets.outlet_key("br.investing.com") != outlets.outlet_key("br.tradingview.com")
    assert outlets.outlet_key("portal.saladanoticia.com.br") == "saladanoticia"


def test_learned_name_replaces_the_domain_and_a_domain_only_outlet_keeps_its_own():
    names = outlets.learn(["Seu Dinheiro", "seudinheiro.com", "br.tradingview.com", "br.investing.com"])
    assert outlets.display("seudinheiro.com", names) == "Seu Dinheiro"
    assert outlets.display("br.investing.com", names) == "br.investing.com"   # never another domain
    assert outlets.display("www.gov.br", names) == "gov.br"
    assert outlets.display("Valor Econômico", names) == "Valor Econômico"     # names untouched


def test_name_in_place_rewrites_news_labels_but_not_official_sources():
    feed = {"feed": [{"citations": [
        {"url": "https://news.google.com/a", "label": "O GLOBO", "via": "Google Notícias"},
        {"url": "https://news.google.com/b", "label": "oglobo.globo.com", "via": "Google Notícias"}]}],
        "sector_events": [{"kind": "sector_event", "outlets": ["oglobo.globo.com"], "sources": [
            {"kind": "official", "url": "https://www.bcb.gov.br/x", "label": "bcb.gov.br"},
            {"kind": "news", "url": "https://news.google.com/c", "publisher": "oglobo.globo.com"}]}]}
    assert outlets.name_in_place(feed) == 2
    assert feed["feed"][0]["citations"][1]["label"] == "O GLOBO"
    ev = feed["sector_events"][0]
    assert ev["sources"][1]["publisher"] == "O GLOBO" and ev["outlets"] == ["O GLOBO"]
    assert ev["sources"][0]["label"] == "bcb.gov.br"
