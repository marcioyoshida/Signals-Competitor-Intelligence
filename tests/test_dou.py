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


# --- #173/#174: the 2026-09-25 online-betting ban (MP 1.394) must be visible -----------------------
import datetime as _dt
import pathlib as _pl

_FX = _pl.Path(__file__).parent / "fixtures" / "dou"
_SEARCH = (_FX / "search_todos_quota_fixa_2026-09-27.html").read_text(encoding="utf-8")
_ACT = (_FX / "act_mp_1394.html").read_text(encoding="utf-8")


def _live_like_fetch(term, section, exact_date):
    # the real Imprensa Nacional behaviour: extra editions only come back for s=todos
    if section == "todos":
        return _SEARCH
    return _SEARCH.replace("DO1_EXTRA_A", "__never_in_do1__") if section == "do1" else ""


def test_old_config_misses_mp_1394_and_new_default_catches_it():
    from src.ingest import dou

    old_organs = tuple(o for o in dou.RELEVANT_ORGANS
                       if o not in ("Presidência da República", "Atos do Poder Executivo",
                                    "Secretaria de Prêmios e Apostas",
                                    "Ministério da Fazenda/Gabinete do Ministro"))
    topics = {"apostas de quota fixa": ["betting"]}
    kw = dict(lookback_days=7, today=_dt.date(2026, 9, 27), pause_sec=0, topic_terms=topics,
              fetcher=_live_like_fetch, act_fetcher=lambda u: _ACT)
    old = dou.fetch_dou([], sections=("do1",), organs=old_organs, **kw)
    assert not any("1.394" in r["title"] for r in old)               # the bug, reproduced
    new = dou.fetch_dou([], **kw)                                     # defaults = the fix
    mp = next(r for r in new if "MEDIDA PROVISÓRIA Nº 1.394" in r["title"])
    assert mp["section"] == "DO1_EXTRA_A" and mp["organ"] == "Atos do Poder Executivo"
    assert mp["industries"] == ["betting"] and mp["company"] is None and mp["name"] is None
    assert mp["full_text"] and "Ficam proibidas, no território nacional" in mp["text"]
    # the organ filter still drops unrelated organs from the same page (noise control)
    assert not any("Ministério Público" in (r["organ"] or "") for r in new)
    assert not any("Planejamento" in (r["organ"] or "") for r in new)
    assert all("<span" not in r["text"] for r in new)                 # snippet markup stripped


def test_topic_terms_have_their_own_budget_and_merge_with_entity_hits():
    from src.ingest import dou

    many = [f"COMPETITOR {i}" for i in range(40)]
    calls = []

    def fetch(term, section, exact_date):
        calls.append(term)
        return _SEARCH if term in ("apostas de quota fixa", "COMPETITOR 0") else ""

    recs = dou.fetch_dou(many, lookback_days=7, today=_dt.date(2026, 9, 27), pause_sec=0,
                         max_terms=5, topic_terms={"apostas de quota fixa": ["betting"]},
                         fetcher=fetch, full_text=False)
    assert calls[0] == "apostas de quota fixa"                       # topics first, never cut
    mp = next(r for r in recs if "1.394" in r["title"])
    assert mp["company"] == "COMPETITOR 0" and mp["industries"] == ["betting"]   # merged


def test_raw_writer_keeps_dou_act_content_for_the_kb():
    from src.ingest import raw_writer

    doc = {"source": "DOU", "kind": "regulatory", "doc_type": "Medida Provisória",
           "title": "MEDIDA PROVISÓRIA Nº 1.394, DE 25 DE SETEMBRO DE 2026", "organ": "Atos do Poder Executivo",
           "section": "DO1_EXTRA_A", "date": "2026-09-25", "industries": ["betting"],
           "text": "Art. 1º Ficam proibidas, no território nacional, a exploração ..."}
    txt = raw_writer._document_text(doc)
    assert "N° None" not in txt and "1.394" in txt and "Ficam proibidas" in txt and "betting" in txt
    assert raw_writer._metadata_attributes(doc)["industries"] == "betting"
