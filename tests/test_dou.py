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
        fetcher=lambda t, s, e: _html([dict(i, content=f"... {t} ...") for i in items]), pause_sec=0,
    )
    ids = {a["id"] for a in acts}
    assert ids == {"dou:portaria-susep-140", "dou:despacho-cade-941"}  # SUSEP + CADE only
    a = next(a for a in acts if a["id"] == "dou:portaria-susep-140")
    assert a["source"] == "DOU"
    assert a["date"] == "2026-08-13"                       # DD/MM/YYYY -> ISO
    assert a["company"] == "BRADESCO"                       # matched term drives entity
    assert a["url"] == "https://www.in.gov.br/web/dou/-/portaria-susep-140"


def test_empty_organ_filter_keeps_all():
    items = [_item("x", "t", RECEITA, "13/08/2026", content="... X S.A. ...")]
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

    # entity 0 must literally occur in the act (#197): "…exploração de loterias de aposta…"
    many = ["LOTERIAS"] + [f"COMPETITOR {i}" for i in range(1, 40)]
    calls = []

    def fetch(term, section, exact_date):
        calls.append(term)
        return _SEARCH if term in ("apostas de quota fixa", "LOTERIAS") else ""

    recs = dou.fetch_dou(many, lookback_days=7, today=_dt.date(2026, 9, 27), pause_sec=0,
                         max_terms=5, topic_terms={"apostas de quota fixa": ["betting"]},
                         fetcher=fetch, full_text=False)
    assert calls[0] == "apostas de quota fixa"                       # topics first, never cut
    mp = next(r for r in recs if "1.394" in r["title"])
    assert mp["company"] == "LOTERIAS" and mp["industries"] == ["betting"]       # merged


def test_raw_writer_keeps_dou_act_content_for_the_kb():
    from src.ingest import raw_writer

    doc = {"source": "DOU", "kind": "regulatory", "doc_type": "Medida Provisória",
           "title": "MEDIDA PROVISÓRIA Nº 1.394, DE 25 DE SETEMBRO DE 2026", "organ": "Atos do Poder Executivo",
           "section": "DO1_EXTRA_A", "date": "2026-09-25", "industries": ["betting"],
           "text": "Art. 1º Ficam proibidas, no território nacional, a exploração ..."}
    txt = raw_writer._document_text(doc)
    assert "N° None" not in txt and "1.394" in txt and "Ficam proibidas" in txt and "betting" in txt
    assert raw_writer._metadata_attributes(doc)["industries"] == "betting"


# --- #188 / #189 / #191 (regulator coverage audit) -----------------------------------------
from src.ingest import registry  # noqa: E402

SUSEP_ORG = "Ministério da Fazenda/Superintendência de Seguros Privados"
SUSEP_AUT = "Ministério da Fazenda/Superintendência de Seguros Privados/Diretoria de Autorizações"
PREVIC_NORMAS = "Ministério da Previdência Social/Superintendência Nacional de Previdência Complementar/Diretoria de  Normas"
PREVIC_LIC = "Ministério da Previdência Social/Superintendência Nacional de Previdência Complementar/Diretoria de Licenciamento"
LEGIS = "Atos do Poder Legislativo"


def _topic_run(items, phrase, industries, **kw):
    return dou.fetch_dou(
        [], today=dt.date(2026, 9, 27), topic_terms={phrase: industries},
        topic_organs={phrase: list(registry.NORMATIVE_ISSUERS)},
        fetcher=lambda t, s, e: _html(items), pause_sec=0, full_text=False, **kw)


def test_laws_and_sector_rules_kept_by_topic_scope():
    items = [
        _item("lc-237", "LEI COMPLEMENTAR Nº 237", LEGIS, "15/09/2026", art="Lei Complementar",
              pub="DO1_EXTRA_C", content="Altera a Lei Complementar nº 229 ... resseguro"),
        _item("res-susep-96", "RESOLUÇÃO SUSEP Nº 96", SUSEP_ORG, "17/09/2026", art="Resolução",
              pub="DO1_EXTRA_D", content="seguros ... resseguro"),
        _item("port-cgaut", "PORTARIA CGAUT/SUSEP nº 182", SUSEP_AUT, "24/09/2026",
              content="autoriza ... resseguro"),                       # per-entity: dropped
        _item("edital-susep", "EDITAL DE CONSULTA PÚBLICA Nº 5", SUSEP_ORG, "17/09/2026",
              art="Edital", pub="DO3", content="resseguro"),            # DO3: dropped
    ]
    ids = {a["id"] for a in _topic_run(items, "resseguro", ["insurance"])}
    assert ids == {"dou:lc-237", "dou:res-susep-96"}


def test_previc_rules_only_from_diretoria_de_normas():
    items = [
        _item("previc-728", "Portaria Previc Nº 728", PREVIC_NORMAS, "17/09/2026",
              pub="DO1_EXTRA_D", content="previdência complementar Plano ASG"),
        _item("previc-647", "Portaria Previc Nº 647", PREVIC_LIC, "17/09/2026",
              content="previdência complementar aprova regulamento"),
    ]
    acts = _topic_run(items, "Portaria Previc", ["closed-pension"])
    assert [a["id"] for a in acts] == ["dou:previc-728"]
    assert acts[0]["normative"] is True        # doc-type-scoped keep → full-text candidate


def test_watched_act_citation_inherits_industries():
    items = [_item("lei-conv", "LEI Nº 15.999", LEGIS, "20/12/2026", art="Lei", pub="DO1",
                   content="Conversão da Medida Provisória nº 1.394, de 25 de setembro de 2026.")]
    acts = dou.fetch_dou(
        [], today=dt.date(2026, 12, 21), topic_terms={"Medida Provisória nº 1.394": ["betting"]},
        topic_organs={"Medida Provisória nº 1.394": list(registry.NORMATIVE_ISSUERS)},
        fetcher=lambda t, s, e: _html(items), pause_sec=0, full_text=False,
        known_instruments={"mp 1.394": ["betting"]})
    assert acts and "betting" in (acts[0].get("industries") or [])
    assert [k for k, _, _ in registry.watched_acts("2026-12-21")] == ["mp 1.394", "mp 1.393"]
    assert registry.watched_acts("2027-04-01") == []


def _days(n, start=dt.date(2026, 9, 26)):
    return [(start - dt.timedelta(days=i)) for i in range(n)]


def test_full_page_is_walked_back_by_date_window():
    # 75 acts on 09-26..09-24 fill page 1; the window page (publishTo = 09-24) returns older ones
    page1 = [_item(f"a{i}", f"PORTARIA {i} BRADESCO", SUSEP_ORG, d.strftime("%d/%m/%Y"))
             for i, d in enumerate([dt.date(2026, 9, 26 - (i // 30)) for i in range(75)])]
    page2 = [_item("a74", "dup", SUSEP_ORG, "24/09/2026"),
             _item("old1", "PORTARIA old1 BRADESCO", SUSEP_ORG, "10/09/2026")]
    calls = []

    def fetcher(t, s, e):
        calls.append(e)
        return _html(page2 if e.startswith("personalizado:") else page1)
    acts = dou.fetch_dou(["BRADESCO"], today=dt.date(2026, 9, 27), fetcher=fetcher,
                         pause_sec=0, full_text=False, classify=False)
    assert calls[0] == "mes|delta=75"
    assert calls[1] == "personalizado:28-08-2026:24-09-2026|delta=75"
    assert len(acts) == 76 and any(a["id"] == "dou:old1" for a in acts)
    assert dou.LAST_STATS["paged"] and not dou.LAST_STATS["saturated"]


def test_saturated_query_is_reported_not_silent():
    def fetcher(t, s, e):  # every window is full and inside the lookback
        base = 26 if not e.startswith("personalizado:") else int(e.split(":")[2][:2])
        return _html([_item(f"{e}-{i}", "P BRADESCO", SUSEP_ORG, f"{base - (i // 40):02d}/09/2026")
                      for i in range(75)])
    dou.fetch_dou(["BRADESCO"], today=dt.date(2026, 9, 27), fetcher=fetcher, pause_sec=0,
                  full_text=False, classify=False, max_pages=2)
    sat = dou.LAST_STATS["saturated"]
    assert sat and sat[0]["term"] == "BRADESCO"


def test_full_text_budget_goes_to_primary_acts_first(monkeypatch):
    monkeypatch.setattr(dou, "FULL_TEXT_MAX_PER_RUN", 1)
    spa = "Ministério da Fazenda/Secretaria de Prêmios e Apostas"
    recs = [{"id": "spa", "organ": spa, "section": "DO1", "url": "u-spa"},
            {"id": "lc", "organ": LEGIS, "section": "DO1_EXTRA_C", "url": "u-lc"}]
    fetched = []
    dou._attach_full_text(recs, lambda u: (fetched.append(u) or
                                           '<div class="texto-dou"><p>corpo</p></div>\n</div>'))
    assert fetched == ["u-lc"] and recs[1]["full_text"]


def test_stemmed_entity_hits_are_dropped_and_not_paged():
    # #197: "CREDITAS" is stem-matched to "crédito(s)" — 75/75 raw hits, MP 1.393 bound to Creditas
    page = [_item(f"c{i}", f"AVISO {i}", "Banco Central do Brasil/Área de Organização", "26/09/2026")
            for i in range(74)] + [_item("mp", "MEDIDA PROVISÓRIA Nº 1.393", LEGIS, "25/09/2026"),
                                   ]
    calls = []

    def fetcher(t, s, e):
        calls.append(e)
        return _html(page)
    acts = dou.fetch_dou(["CREDITAS"], today=dt.date(2026, 9, 27), fetcher=fetcher, pause_sec=0,
                         full_text=False, classify=False)
    assert acts == [] and len(calls) == 1          # nothing bound, no walk-back
    assert dou.LAST_STATS["fuzzy"][0]["term"] == "CREDITAS" and not dou.LAST_STATS["saturated"]


def test_literal_hit_is_accent_and_case_folded_whole_word():
    rec = {"title": "EXTRATO", "text": "... a Creditás Soluções Financeiras Ltda. ..."}
    assert dou.literal_hit(rec, "CREDITAS")
    assert not dou.literal_hit({"text": "operações de créditos consignados"}, "CREDITAS")
    assert not dou.literal_hit({"text": "NUBANKING"}, "NUBANK")
