import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import dou


def _html(items):
    blob = json.dumps({"jsonArray": items})
    return f'<html><body><script type="application/json" id="_x_params">{blob}</script></body></html>'


def _item(slug, title, organ, pubdate, content="", art="Portaria", pub="DO1"):
    return {
        "pubName": pub, "urlTitle": slug, "title": title, "content": content,
        "pubDate": pubdate, "artType": art, "hierarchyStr": organ,
    }


SUSEP = "Ministério da Fazenda/Superintendência de Seguros Privados/Coordenação-Geral de Autorizações"
CADE = "Ministério da Justiça/Conselho Administrativo de Defesa Econômica/Superintendência-Geral"
RECEITA = "Ministério da Fazenda/Secretaria Especial da Receita Federal/Delegacia de Julgamento"


def test_parses_filters_organ_and_date():
    items = [
        _item("portaria-susep-140", "PORTARIA CGAUT/SUSEP nº 140", SUSEP, "13/08/2026"),
        _item("despacho-cade-941", "DESPACHO SG Nº 941", CADE, "10/08/2026", art="Despacho"),
        _item("pauta-julgamento-1", "PAUTA DE JULGAMENTO", RECEITA, "12/08/2026"),   # noise organ
        _item("portaria-antiga", "PORTARIA velha", SUSEP, "01/01/2026"),             # too old
    ]
    acts = dou.fetch_dou(
        ["BRADESCO"], lookback_days=30, today=dt.date(2026, 8, 16),
        fetcher=lambda t, s, e: _html(items), pause_sec=0,
    )
    ids = {a["id"] for a in acts}
    assert ids == {"dou:portaria-susep-140", "dou:despacho-cade-941"}  # SUSEP + CADE only
    a = next(a for a in acts if a["id"] == "dou:portaria-susep-140")
    assert a["source"] == "DOU"
    assert a["date"] == "2026-08-13"                       # DD/MM/YYYY -> ISO
    assert a["company"] == "BRADESCO"                       # matched term drives entity
    assert a["url"] == "https://www.in.gov.br/web/dou/-/portaria-susep-140"


def test_empty_organ_filter_keeps_all():
    items = [_item("x", "t", RECEITA, "13/08/2026")]
    acts = dou.fetch_dou(
        ["X"], lookback_days=30, today=dt.date(2026, 8, 16), organs=(),
        fetcher=lambda t, s, e: _html(items), pause_sec=0,
    )
    assert len(acts) == 1


def test_malformed_html_degrades_to_empty():
    acts = dou.fetch_dou(
        ["X"], today=dt.date(2026, 8, 16),
        fetcher=lambda t, s, e: "<html>no params here</html>", pause_sec=0,
    )
    assert acts == []
