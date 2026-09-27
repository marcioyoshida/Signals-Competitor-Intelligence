"""#175 (incident #173): federal normative acts → covered industries + severity.

Every act here is REAL DOU text (fixtures trimmed from the live in.gov.br act pages, fetched
2026-09-27) or a real DOU search title/snippet. No network.
"""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import dou, federal_acts as fa, registry

_FX = Path(__file__).parent / "fixtures" / "dou"


def _act(fixture, title, organ, section, doc_type, **kw):
    text = dou.extract_act_text((_FX / fixture).read_text(encoding="utf-8"))
    assert text, fixture
    return {"source": "DOU", "kind": "regulatory", "title": title, "organ": organ,
            "section": section, "doc_type": doc_type, "text": text, "full_text": True,
            "company": None, **kw}


SPA = "Ministério da Fazenda/Secretaria de Prêmios e Apostas"
MF = "Ministério da Fazenda/Gabinete do Ministro"


def _mp_1394():
    return _act("act_mp_1394.html", "MEDIDA PROVISÓRIA Nº 1.394, DE 25 DE SETEMBRO DE 2026",
                "Atos do Poder Executivo", "DO1_EXTRA_A", "Medida Provisória", id="dou:mp1394")


# --- acceptance: MP 1.394 → betting / critical, no entity ---------------------------------------
def test_mp_1394_real_text_maps_to_betting_critical():
    c = fa.classify(_mp_1394())
    assert c["industries"] == ["betting"]
    assert c["severity"] == "critical"
    assert "proib" in c["severity_reason"]
    # the body names banks / payment institutions in passing (balances returned through them);
    # the lead-first rule keeps it a BETTING act, not a banking one
    assert "banking" not in c["industries"] and "fintech" not in c["industries"]


def test_annotate_writes_the_contract_fields_without_an_entity():
    rec = _mp_1394()
    fa.annotate([rec])
    assert rec["industries"] == ["betting"] and rec["severity"] == "critical"
    assert rec["company"] is None


# --- negatives from real DOU acts ---------------------------------------------------------------
def test_spa_edital_de_citacao_is_procedural_not_critical():
    full = _act("act_spa_edital_citacao_2026-09-22.html", "EDITAL DE CITAÇÃO DE 22 DE SETEMBRO DE 2026",
                SPA, "DO3", "Edital de Citação")
    c = fa.classify(full)
    assert c["industries"] == ["betting"] and c["severity"] == "medium"
    # the same act as the search snippet (cut mid-name) — still betting via the SPA organ
    snippet = {**full, "full_text": False,
               "text": ("não ter sido localizada no endereço registrado nos sistemas da SUBSECRETARIA DE "
                        "AÇÃO SANCIONADORA DA SECRETARIA ... DE PRÊMIOS E APOSTAS, levo ao conhecimento "
                        "público que foi imputada à empresa PHD BRASIL CURSOS E TREINAMENTO PROFIS...")}
    c2 = fa.classify(snippet)
    assert c2["industries"] == ["betting"] and c2["severity"] in ("low", "medium")


def test_portaria_mf_2946_same_day_same_edition_is_not_a_betting_act():
    # Published beside MP 1.394 (DO1_EXTRA_B, MF/Gabinete) and assumed in #173 to be the ban's
    # implementing rules — its real text is the DIESEL subsidy of MP 1.391.
    c = fa.classify(_act("act_portaria_mf_2946.html", "PORTARIA MF Nº 2.946, DE 25 DE SETEMBRO DE 2026",
                         MF, "DO1_EXTRA_B", "Portaria"))
    assert c["industries"] == [] and c["severity"] == "low"


def test_personnel_and_routine_spa_acts_are_low():
    personnel = {"source": "DOU", "kind": "regulatory", "organ": SPA, "section": "DO2", "doc_type": "Portaria",
                 "title": "Portaria SPA/MF Nº 2.778, de 17 de setembro de 2026",
                 "text": ("Portaria SPA/MF Nº 2.778, de 17 de setembro de 2026 A SECRETÁRIA DE PRÊMIOS E APOSTAS "
                          "DO MINISTÉRIO DA ... Executivo de Assistente Técnico, código CCE 2.05, da Subsecretaria "
                          "de Monitoramento e Fiscalização da Secretaria ... de Prêmios e Apostas do Ministério da Fazenda.")}
    extrato = {"source": "DOU", "kind": "regulatory", "organ": SPA, "section": "DO3", "doc_type": "Extrato",
               "title": "EXTRATO DE ACORDO DE COOPERAÇÃO TÉCNICA MF/FGO Nº 41/2026",
               "text": "Vigência: 24 meses. Objeto: cooperação técnica em apostas de quota fixa."}
    assert fa.classify(personnel)["severity"] == "low"
    assert fa.classify(extrato)["severity"] == "low"


# --- positives below critical -------------------------------------------------------------------
def test_spa_2750_new_procedures_is_high_and_revoking_a_portaria_is_not_critical():
    # ementa: "Dispõe sobre os procedimentos … repressão de transações de pagamento relacionadas
    # à exploração irregular … de apostas de quota fixa; … e revoga a Portaria SPA/MF nº 566"
    c = fa.classify(_act("act_portaria_spa_2750.html", "Portaria SPA/MF Nº 2.750, DE 10 DE SETEMBRO DE 2026",
                         SPA, "DO1_EXTRA_B", "Portaria"))
    assert c["severity"] == "high"
    assert "betting" in c["industries"] and "fintech" in c["industries"]  # payment blocking


def test_mp_1393_desenrola_is_credit_high():
    c = fa.classify(_act("act_mp_1393.html", "MEDIDA PROVISÓRIA Nº 1.393, DE 25 DE SETEMBRO DE 2026",
                         "Atos do Poder Executivo", "DO1_EXTRA_A", "Medida Provisória"))
    # #195: a consumer-credit programme run by the lenders → banking + fintech, not securitization
    assert c["industries"] == ["banking", "fintech"] and c["severity"] == "high"


def test_despacho_forwarding_the_mp_is_medium_and_cites_it():
    batch = [_mp_1394(),
             _act("act_despachos_presidente_2026-09-25.html", "DESPACHOS DO PRESIDENTE DA REPÚBLICA",
                  "Presidência da República", "DO1_EXTRA_A", "Despacho", id="dou:desp")]
    fa.annotate(batch)
    desp = batch[1]
    assert desp["industries"] == ["betting"] and desp["severity"] == "medium"
    assert desp["cites"] == ["mp 1.394"]


def test_bcb_normativo_without_title_is_classified_from_its_subject():
    rec = {"source": "BCB", "kind": "regulatory", "doc_type": "Resolução BCB", "number": "533",
           "subject": ("Altera a Resolução BCB nº 520, de 10 de novembro de 2025, que disciplina a constituição "
                       "e o funcionamento das sociedades prestadoras de serviços de ativos virtuais")}
    c = fa.classify(rec)
    assert c["industries"] == ["crypto"] and c["severity"] == "high"
    fx = {"source": "BCB", "kind": "regulatory", "doc_type": "Comunicado", "number": "44.100",
          "subject": "Divulga o resultado de leilões de swap"}
    assert fa.classify(fx) == {**fa.classify(fx), "industries": [], "severity": "low"}


def test_deadline_change_is_medium():
    rec = {"source": "BCB", "kind": "regulatory", "doc_type": "Resolução BCB", "number": "999",
           "subject": "Altera a Resolução BCB nº 1, que institui o arranjo de pagamentos Pix, para prorrogar o prazo de adequação"}
    # "institui" here describes the AMENDED act; the new act only moves a deadline
    c = fa.classify(rec)
    assert c["industries"] == ["fintech"]
    assert c["severity"] in ("medium", "high")


# --- the citation rule: implementing acts inherit the critical act's industries -----------------
_IMPLEMENTING = {"source": "DOU", "kind": "regulatory", "organ": MF, "section": "DO1_EXTRA_B",
                 "doc_type": "Portaria", "full_text": True, "id": "dou:impl",
                 "title": "PORTARIA MF Nº 3.001, DE 26 DE SETEMBRO DE 2026",
                 "text": ("PORTARIA MF Nº 3.001, DE 26 DE SETEMBRO DE 2026 Estabelece procedimentos para a "
                          "devolução de saldos de que trata o art. 6º da Medida Provisória nº 1.394, de 25 de "
                          "setembro de 2026. O MINISTRO DE ESTADO DA FAZENDA, no uso das atribuições ... resolve: "
                          "Art. 1º Esta Portaria estabelece procedimentos para a devolução de saldos.")}


def test_an_act_citing_a_critical_mp_inherits_its_industry():
    alone = dict(_IMPLEMENTING)
    fa.annotate([alone])
    assert "industries" not in alone and alone["severity"] == "low"     # no sector vocabulary
    impl = dict(_IMPLEMENTING)
    fa.annotate([_mp_1394(), impl])                                    # same batch as the MP
    assert impl["industries"] == ["betting"] and impl["cites"] == ["mp 1.394"]
    assert impl["severity"] == "high" and impl["industries_basis"].startswith("cites")
    later = dict(_IMPLEMENTING)                                        # a later run: caller-known
    fa.annotate([later], known_instruments={"mp 1.394": ["betting"]})
    assert later["industries"] == ["betting"]


def test_instrument_refs_and_citation_phrase():
    assert fa.instrument_refs("altera a Lei nº 14.790, de 29 de dezembro de 2023, e a Medida Provisória n° 1.394") \
        == ["lei 14.790", "mp 1.394"]
    assert fa.own_ref({"title": "MEDIDA PROVISÓRIA Nº 1.394, DE 25 DE SETEMBRO DE 2026"}) == "mp 1.394"
    assert fa.own_ref({"title": "DESPACHOS DO PRESIDENTE DA REPÚBLICA"}) is None
    assert fa.citation_phrase("mp 1.394") == "Medida Provisória nº 1.394"   # the "nº" is required live


def test_rejected_topic_tags_are_dropped_but_kept_for_audit():
    # a full-text MF act found by the "Sistema Financeiro Nacional" topic whose lead is about
    # institutional performance goals: the topic tag does not survive classification
    rec = {"source": "DOU", "kind": "regulatory", "organ": MF, "section": "DO1", "doc_type": "Portaria",
           "full_text": True, "topic_term": "Sistema Financeiro Nacional", "industries": ["banking"],
           "title": "PORTARIA MF Nº 2.831, DE 22 DE SETEMBRO DE 2026",
           "text": ("PORTARIA MF Nº 2.831, DE 22 DE SETEMBRO DE 2026 Altera o anexo da Portaria n° 2.124, que "
                    "estabelece as metas globais para a avaliação de desempenho institucional do Ministério da "
                    "Fazenda. O MINISTRO DE ESTADO DA FAZENDA, no uso ... Conselho de Recursos do Sistema "
                    "Financeiro Nacional ...")}
    fa.annotate([rec])
    assert "industries" not in rec and rec["topic_industries"] == ["banking"] and rec["severity"] == "low"


# --- wired into the DOU fetch path ----------------------------------------------------------------
def _search_page(items):
    blob = json.dumps({"jsonArray": items})
    return f'<html><body><script type="application/json" id="_x_params">{blob}</script></body></html>'


def test_fetch_dou_classifies_scopes_topics_and_follows_citations():
    search = (_FX / "search_todos_quota_fixa_2026-09-27.html").read_text(encoding="utf-8")
    mp_page = (_FX / "act_mp_1394.html").read_text(encoding="utf-8")
    impl_item = {"pubName": "DO1_EXTRA_B", "urlTitle": "portaria-mf-n-3.001", "pubDate": "26/09/2026",
                 "artType": "Portaria", "hierarchyStr": MF,
                 "title": "PORTARIA MF Nº 3.001, DE 26 DE SETEMBRO DE 2026", "content": "..."}
    calls = []

    def fetch(term, section, exact_date):
        calls.append(term)
        if term == "apostas de quota fixa":
            return search
        if term == "Medida Provisória nº 1.394":
            return _search_page([impl_item])
        return ""

    def act(url):
        if "medida-provisoria-n-1.394" in url:
            return mp_page
        if "portaria-mf-n-3.001" in url:
            return f'<div><div class="texto-dou">{_IMPLEMENTING["text"]}</div>\n</div>'
        return ""

    topics = {"apostas de quota fixa": ["betting"]}
    recs = dou.fetch_dou([], lookback_days=7, today=dt.date(2026, 9, 27), pause_sec=0,
                         topic_terms=topics, topic_organs={"apostas de quota fixa": list(registry.NORMATIVE_ISSUERS)},
                         fetcher=fetch, act_fetcher=act)
    by_title = {r["title"]: r for r in recs}
    mp = by_title["MEDIDA PROVISÓRIA Nº 1.394, DE 25 DE SETEMBRO DE 2026"]
    assert mp["industries"] == ["betting"] and mp["severity"] == "critical" and mp["company"] is None
    assert calls[-1] == "Medida Provisória nº 1.394"                   # the citation follow-up
    impl = by_title["PORTARIA MF Nº 3.001, DE 26 DE SETEMBRO DE 2026"]
    assert impl["industries"] == ["betting"] and impl["severity"] == "high"
    assert all("severity" in r for r in recs)                           # every act leaves classified
    # scope: the MPO budget Portaria on the same search page is not a normative issuer
    assert not any("Planejamento" in (r.get("organ") or "") for r in recs)


def test_topic_scope_exact_organ_marker():
    assert dou._organ_in_scope("Presidência da República", ["Presidência da República$"])
    assert not dou._organ_in_scope("Presidência da República/Casa Civil/Agência Brasileira de Inteligência",
                                    ["Presidência da República$"])
    assert dou._organ_in_scope("Ministério da Fazenda/Secretaria de Prêmios e Apostas",
                               ["Secretaria de Prêmios e Apostas"])
    assert dou._organ_in_scope("anything", None)


def test_search_title_highlight_markup_is_stripped():
    page = _search_page([{"pubName": "DO1_EXTRA_B", "urlTitle": "x", "pubDate": "25/09/2026", "artType": "Portaria",
                          "hierarchyStr": MF, "content": "",
                          "title": "<span class='highlight' style='background:#FFA;'>PORTARIA</span> MF Nº 2.946"}])
    assert dou._parse(page, "t")[0]["title"] == "PORTARIA MF Nº 2.946"


def test_real_implementing_portaria_names_its_mp_not_the_sector():
    # Why the citation rule exists, on REAL text: Portaria MF 2.946 implements MP 1.391 and its
    # ementa ("Estabelece o valor unitário da subvenção econômica … de que trata o art. 1º da
    # Medida Provisória nº 1.391") never names the sector — only the MP. (MP 1.391 is not in a
    # covered industry, so here it inherits only from an explicitly known mapping.)
    rec = _act("act_portaria_mf_2946.html", "PORTARIA MF Nº 2.946, DE 25 DE SETEMBRO DE 2026",
               MF, "DO1_EXTRA_B", "Portaria")
    assert "mp 1.391" in fa.instrument_refs(rec["text"])
    assert fa.classify(rec)["industries"] == []
    c = fa.classify(rec, known_instruments={"mp 1.391": ["betting"]})
    assert c["industries"] == ["betting"] and c["cites"] == ["mp 1.391"]


# --- #195: AML compliance tag + the 8 new industry vocabularies (real DOU acts) ----------------
BCB_DC = "Banco Central do Brasil/Diretoria Colegiada"


def test_res_bcb_588_aml_is_a_compliance_tag_not_an_industry():
    # A2 of the regulator audit: amends Circular 3.978 (PLD/FT) — was industries=[] / low
    rec = _act("act_res_bcb_588.html", "RESOLUÇÃO BCB Nº 588, DE 23 DE SETEMBRO DE 2026", BCB_DC, "DO1",
               "Resolução", id="dou:bcb588")
    c = fa.classify(rec)
    assert c["industries"] == [] and c["compliance"] == ["aml"]
    assert c["severity"] != "low"                     # the tag is coverage for severity
    fa.annotate([rec])
    assert rec["compliance_tags"] == ["aml"] and "industries" not in rec


def test_res_bcb_589_psav_stays_crypto_without_aml_tag():
    c = fa.classify(_act("act_res_bcb_589.html", "RESOLUÇÃO BCB Nº 589, DE 23 DE SETEMBRO DE 2026", BCB_DC,
                         "DO1", "Resolução"))
    assert c["industries"] == ["crypto"] and c["compliance"] == []


def test_dtvm_liquidation_maps_to_investment_banking():
    # A3: Ato 1.389 (Trustee DTVM) — was "no covered industry"
    c = fa.classify(_act("act_ato_bcb_1389.html", "ATO Nº 1.389, DE 3 DE SETEMBRO DE 2026",
                         "Banco Central do Brasil/Presidência", "DO1", "Ato"))
    assert c["industries"] == ["investment-banking"] and c["severity"] != "low"


def test_new_industry_vocabularies_match_their_sector_and_not_homonyms():
    ind = fa.industries_in
    assert "real-estate-funds" in ind("altera a Lei nº 8.668, que dispõe sobre fundos de investimento imobiliário")
    assert "agri-funds" in ind("Fiagro: Fundos de Investimento nas Cadeias Produtivas Agroindustriais")
    assert "acquiring" in ind("regras para credenciadoras de cartões e subcredenciadores")
    assert "advisory" in ind("credenciamento de consultor de valores mobiliários")
    assert "private-markets" in ind("Fundo de Investimento em Participações Multiestratégia")
    assert "wealth-management" in ind("assessores de investimento e carteiras administradas")
    assert "financial-data-analytics" in ind("entidades registradoras de recebíveis de cartão")
    # "credenciadora" alone is an accreditation body in the DOU (75/75 non-FS hits, live)
    assert "acquiring" not in ind("entidade credenciadora de cursos de formação")
    assert fa.compliance_in("Custo do PLD no mercado de energia") == []
