"""#193 — CCO sanctions/enforcement register (audit R8/R10, false-coverage #9).

Fixtures are the real items found in the 2026-08..09 digests: BCB Ato do Presidente 1.390 +
Comunicado 45865 (liquidação of Banvox DTVM), the SPA editais de citação of 22/09, the CVM PAS
headlines against Banco Master (08/09), the MPF/Receita operation against Betnacional's owner
(28/08), and the CEIS rows mirrored in sanctions/index.json."""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import federal_acts as fa
from src.synth import enforcement as enf
from src.synth import executive

TODAY = dt.date(2026, 9, 27)


def _bcb(doc_type, number, subject, date="2026-09-03"):
    return {"id": f"bcb:{doc_type}:{number}", "source": "BCB", "kind": "regulatory",
            "doc_type": doc_type, "number": number, "date": date, "subject": subject,
            "url": f"https://www.bcb.gov.br/estabilidadefinanceira/exibenormativo?tipo={doc_type}&numero={number}"}


ATO_1390 = _bcb("Ato do Presidente", "1390",
                "Decreta a liquidação extrajudicial da Banvox Distribuidora de Títulos e Valores Mobiliários Ltda.")
COM_45865 = _bcb("Comunicado", "45865",
                 "Comunica a decretação da liquidação extrajudicial da Banvox Distribuidora de Títulos e Valores "
                 "Mobiliários Ltda., a nomeação da liquidante extrajudicial e a indisponibilidade dos bens dos "
                 "controladores e dos ex-administradores da instituição.")
ATO_1389 = _bcb("Ato do Presidente", "1389",
                "Decreta a liquidação extrajudicial da Trustee Distribuidora de Títulos e Valores Mobiliários Ltda.")


def _spa(n, party):
    return {"id": f"dou:edital-de-citacao-{n}", "source": "DOU", "kind": "regulatory",
            "doc_type": "Edital de Citação", "organ": "Ministério da Fazenda/Secretaria de Prêmios e Apostas",
            "title": "EDITAL DE CITAÇÃO DE 22 DE SETEMBRO DE 2026", "section": "DO3",
            "text": ("não ter sido localizada no endereço registrado nos sistemas da SUBSECRETARIA DE AÇÃO "
                     f"SANCIONADORA DA SECRETARIA ... DE PRÊMIOS E APOSTAS, levo ao conhecimento público que "
                     f"foi imputada à empresa {party}..."),
            "date": "2026-09-24", "url": f"https://www.in.gov.br/web/dou/-/edital-{n}",
            "industries": ["betting"], "severity": "medium", "company": "BRADESCO"}


def _news(i, title, publisher, company="Banco Master", date="2026-09-08"):
    return {"id": f"news:{i}", "source": "News", "kind": "competitor", "publisher": publisher,
            "title": title, "company": company, "date": date, "url": f"https://news.example/{i}"}


CVM_NEWS = [
    _news(1, "CVM multa Banco Master, Daniel Vorcaro e outros réus em R$ 203 milhões por fraude", "Estadão"),
    _news(2, "CVM condena Banco Master e Daniel Vorcaro por fraude com cotas de FII; multas somam R$ 203 milhões", "G1"),
    _news(3, "CVM condena Banco Master e Daniel Vorcaro por fraude", "CBN"),
]


# --- federal_acts severity rules --------------------------------------------------------------
def test_liquidation_decree_is_operator_level_critical_with_target():
    c = fa.classify(ATO_1389)
    assert c["severity"] == "critical" and "liquidação extrajudicial" in c["severity_reason"]
    e = fa.enforcement_of(ATO_1389)
    assert e["kind"] == "liquidacao"
    assert e["target"] == "Trustee Distribuidora de Títulos e Valores Mobiliários Ltda."


def test_comunicado_naming_the_liquidator_is_not_a_personnel_act():
    c = fa.classify(COM_45865)
    assert c["severity"] == "critical", c
    assert fa.enforcement_of(COM_45865)["target"].startswith("Banvox Distribuidora")


def test_target_keeps_sa_and_commas_inside_a_name():
    rec = _bcb("Ato do Presidente", "1388",
               "Decreta a liquidação extrajudicial da Simpala S.A. Crédito, Financiamento e Investimento.")
    assert fa.enforcement_of(rec)["target"] == "Simpala S.A. Crédito, Financiamento e Investimento"


def test_raet_intervencao_and_cassacao():
    raet = _bcb("Ato do Presidente", "9", "Decreta o Regime de Administração Especial Temporária (RAET) no Banco XYZ S.A.")
    interv = _bcb("Ato do Presidente", "10", "Decreta a intervenção no Banco Exemplo S.A. e nomeia interventor.")
    cass = _bcb("Ato do Presidente", "11", "Cassa a autorização para funcionamento da Corretora Alfa Ltda.")
    assert (fa.enforcement_of(raet)["kind"], fa.enforcement_of(raet)["target"]) == ("raet", "Banco XYZ S.A.")
    assert fa.enforcement_of(interv)["target"] == "Banco Exemplo S.A."
    assert fa.enforcement_of(cass)["kind"] == "cassacao" and fa.enforcement_of(cass)["target"] == "Corretora Alfa Ltda."
    for r in (raet, interv, cass):
        assert fa.classify(r)["severity"] == "critical"


def test_non_decree_act_about_one_liquidation_is_high():
    rec = _bcb("Comunicado", "1", "Prorroga o prazo da liquidação extrajudicial do Banco Master S.A.")
    assert fa.classify(rec)["severity"] == "high"
    assert fa.enforcement_of(rec)["target"] == "Banco Master S.A."


def test_framework_and_unrelated_liquidacao_are_not_enforcement():
    fw = _bcb("Resolução CMN", "3", "Dispõe sobre o regime de liquidação extrajudicial das instituições financeiras.")
    rural = _bcb("Resolução CMN", "5", "linha de crédito rural para liquidação ou amortização de operações")
    inq = _bcb("Comunicado", "45851", "Prorroga o prazo para conclusão de inquérito")
    swap = _bcb("Comunicado", "45872", "Divulga o resultado de leilões de swap; intervenção no mercado de câmbio")
    for r in (fw, rural, inq, swap):
        assert fa.enforcement_of(r) is None, r["subject"]
    assert fa.classify(fw)["severity"] != "critical"


# --- classifiers ---------------------------------------------------------------------------------
def test_spa_edital_is_a_sanction_proceeding_bound_to_the_legal_person_only():
    a = enf.official_action(_spa(1, "4ALL COMUNICAÇÃO LTDA"))
    assert a["kind"] == "processo_sancionador" and a["authority"] == "SPA/MF"
    assert a["target"] == "4ALL COMUNICAÇÃO LTDA" and a["severity"] == "medium"
    # a natural person (an influencer) is out of scope, like CEIS/CNEP pessoa física
    assert enf.official_action(_spa(2, "AMANDA JOYCE SOARESEDITAL DE C")) is None


def test_routine_bcb_comunicado_is_not_enforcement():
    assert enf.official_action(_bcb("Comunicado", "45867", "Divulga a Taxa Básica Financeira (TBF)")) is None


def test_news_classifier():
    a = enf.news_action(CVM_NEWS[0])
    assert (a["kind"], a["authority"], a["target"]) == ("sancao", "CVM", "Banco Master")
    op = enf.news_action(_news(9, "MPF e Receita fazem operação contra dona da Betnacional por suspeita de lavagem",
                               "O Globo", company="Betnacional", date="2026-08-28"))
    assert (op["kind"], op["authority"]) == ("inquerito", "MPF")
    # hypothetical / political framing, and a headline not naming the company
    assert enf.news_action(_news(10, "Deputada pede convocação de Nikolas para depor no inquérito do Banco Master",
                                 "Folha")) is None
    assert enf.news_action(_news(11, "CVM multa gestora por fraude", "G1")) is None
    procon = enf.news_action(_news(12, "Procon-MPMG multa Banco Daycoval em R$ 1,58 milhão", "O Tempo",
                                   company="Banco Daycoval"))
    assert procon["authority"] == "Procon"


# --- register ------------------------------------------------------------------------------------
def test_ato_and_comunicado_merge_into_one_official_critical_action():
    store, rep = enf.build_register({}, [ATO_1390, COM_45865, ATO_1389], [], today=TODAY)
    acts = store["actions"]
    assert len(acts) == 2 and rep["official"] == 3
    banvox = next(a for a in acts if "Banvox" in (a["target"] or ""))
    assert banvox["severity"] == "critical" and banvox["confidence"] == "official"
    assert banvox["authority"] == "BCB" and banvox["kind_label"] == "liquidação extrajudicial"
    assert len(banvox["sources"]) == 2 and banvox["entity"] is None
    assert banvox["id"].startswith("enf:liquidacao:")


def test_news_corroboration_and_single_outlet_cap():
    store, _ = enf.build_register({}, [], CVM_NEWS, today=TODAY)
    (a,) = store["actions"]
    assert a["confidence"] == "corroborated" and a["n_outlets"] == 3 and a["severity"] == "high"
    liq = _news(20, "BC decreta liquidação extrajudicial do Banco Pleno", "Valor", company="Banco Pleno",
                date="2026-09-20")
    store, _ = enf.build_register({}, [], [liq], today=TODAY)
    assert store["actions"][0]["severity"] == "high"  # one outlet never makes it critical
    assert store["actions"][0]["confidence"] == "reported"


def test_accumulates_across_runs_without_duplicating_sources():
    store, _ = enf.build_register({}, [], CVM_NEWS[:1], today=dt.date(2026, 9, 8))
    assert store["actions"][0]["confidence"] == "reported"
    store, rep = enf.build_register(store, [], CVM_NEWS, today=dt.date(2026, 9, 9))
    (a,) = store["actions"]
    assert a["confidence"] == "corroborated" and len(a["sources"]) == 3 and not rep["created"]


def test_resolver_binds_only_an_unambiguous_entity():
    store, _ = enf.build_register({}, [], CVM_NEWS[:1], resolver=lambda it: ["master"], today=TODAY)
    assert store["actions"][0]["entity"] == "master"
    store, _ = enf.build_register({}, [], CVM_NEWS[:1], resolver=lambda it: ["a", "b"], today=TODAY)
    assert store["actions"][0]["entity"] is None


def test_dou_search_term_company_is_never_used_to_bind():
    store, _ = enf.build_register({}, [_spa(1, "4ALL COMUNICAÇÃO LTDA")], [], today=TODAY)
    a = store["actions"][0]
    assert a["entity"] is None and a["industries"] == ["betting"]
    assert "4ALL COMUNICAÇÃO LTDA" in a["title"]


def test_sanction_registry_grouped_and_mirrored():
    rows = [
        {"id": "sanctions:bk:ceis:1", "entity": "bk", "cadastro": "CEIS", "company": "BK IP S A",
         "category": "Impedimento/proibição de contratar com prazo determinado", "orgao": "SENADO FEDERAL",
         "start": "2026-04-28", "end": "2026-12-08", "url": "https://portaldatransparencia.gov.br/sancoes/ceis"},
        {"id": "sanctions:bk:ceis:2", "entity": "bk", "cadastro": "CEIS", "company": "BK IP S A",
         "category": "Suspensão", "orgao": "TJRJ", "start": "2026-03-23", "end": "2027-03-23"},
        {"id": "sanctions:bk:ceis:3", "entity": "bk", "cadastro": "CEIS", "company": "BK IP S A",
         "category": "Suspensão", "orgao": "Velho", "start": "2020-01-01", "end": "2021-01-01"},  # expired
        {"id": "sanctions:x:cnep:1", "entity": "x", "cadastro": "CNEP", "company": "X SA",
         "category": "Multa - Lei Anticorrupção", "orgao": "CGU", "start": "2026-05-01", "end": None},
    ]
    store, _ = enf.build_register({}, [], [], rows, today=TODAY)
    by = {a["entity"]: a for a in store["actions"]}
    assert by["bk"]["n_sanctions"] == 2 and by["bk"]["kind"] == "impedimento"
    assert by["bk"]["date"] == "2026-04-28" and by["bk"]["confidence"] == "official"
    assert by["x"]["kind"] == "anticorrupcao" and by["x"]["severity"] == "high"
    # re-mirroring replaces (not duplicates) the registry-derived actions
    store, _ = enf.build_register(store, [], [], rows[:1], today=TODAY)
    assert [a["entity"] for a in store["actions"]] == ["bk"]


def test_split_digest_reads_items_and_context():
    digest = {"regulatory": {"items": [ATO_1390], "context": [COM_45865, ATO_1390]},
              "news": {"items": CVM_NEWS[:1], "context": []},
              "sanctions": {"items": [{"id": "s1", "kind": "sanction"}]}}
    o, n, s = enf.split_digest(digest)
    assert len(o) == 2 and len(n) == 1 and len(s) == 1


def test_for_feed_window_order_and_roster_industries():
    store, _ = enf.build_register({}, [ATO_1390, COM_45865], CVM_NEWS, resolver=None, today=TODAY)
    store["actions"].append({**store["actions"][0], "id": "enf:old", "last_evidence": "2025-01-01",
                             "date": "2025-01-01"})
    store["actions"][-1]["entity"] = None
    rows = enf.for_feed(store, today=TODAY)
    assert [r["severity"] for r in rows] == ["critical", "high"] and "subject_key" not in rows[0]
    store["actions"][1]["entity"] = "master"
    rows = enf.for_feed(store, entity_attrs={"master": {"label": "Banco Master", "industries": ["banking"]}},
                        today=TODAY)
    assert rows[1]["industries"] == ["banking"] and rows[1]["label"] == "Banco Master"


def test_scope_fails_closed_for_bound_rows_and_keeps_public_untagged_acts():
    acts = [{"id": "a", "entity": "itau", "industries": ["banking"]},
            {"id": "b", "entity": None, "industries": []},
            {"id": "c", "entity": None, "industries": ["betting"]}]
    kept = enf.scope(acts, ["banking"], lambda r: r.get("entity") == "itau")
    assert [a["id"] for a in kept] == ["a", "b"]
    assert [a["id"] for a in enf.scope(acts, ["fintech"], lambda r: False)] == ["b"]


class FakeS3:
    def __init__(self, store=None):
        self.store = dict(store or {})

    def get_object(self, Bucket, Key):
        if Key not in self.store:
            raise KeyError(Key)
        return {"Body": _Body(self.store[Key])}

    def put_object(self, Bucket, Key, Body, **kw):
        self.store[Key] = Body


class _Body:
    def __init__(self, b):
        self._b = b

    def read(self):
        return self._b


def test_update_from_digest_persists_and_mirrors_ceis():
    idx = {"records": {"r1": {"id": "sanctions:bk:ceis:1", "entity": "bk", "cadastro": "CEIS",
                              "company": "BK", "category": "Suspensão", "start": "2026-08-07",
                              "end": "2027-08-06"}}}
    s3 = FakeS3({"sanctions/index.json": json.dumps(idx).encode()})
    out = enf.update_from_digest({"regulatory": {"items": [ATO_1390], "context": [COM_45865]},
                                  "news": {"items": CVM_NEWS}}, "b", resolver=lambda it: [], s3=s3,
                                 today=TODAY)
    assert out["actions"] == 3 and out["official_items"] == 2 and out["news_items"] == 3
    saved = json.loads(s3.store[enf.STORE_KEY])
    assert {a["kind"] for a in saved["actions"]} == {"liquidacao", "sancao", "impedimento"}


# --- surfaces ------------------------------------------------------------------------------------
def _exec_feed():
    store, _ = enf.build_register({}, [ATO_1390, COM_45865], CVM_NEWS,
                                  [{"id": "s1", "entity": "itau", "cadastro": "CNEP", "company": "ITAU",
                                    "category": "Multa", "orgao": "CGU", "start": "2026-09-01"}],
                                  today=TODAY)
    attrs = {"itau": {"label": "Itaú", "industries": ["banking"]},
             "bet1": {"label": "Bet Um", "industries": ["betting"]}}
    return {"as_of": "2026-09-27", "dates": ["2026-09-27"],
            "industry_options": [{"slug": "banking", "display_name": "Banking"},
                                 {"slug": "betting", "display_name": "Apostas"}],
            "entity_attrs": attrs, "feed": [], "integrity": {"findings": []},
            "enforcement": enf.for_feed(store, entity_attrs=attrs, today=TODAY)}


def test_build_cco_enforcement_panel_counts_risk_register_and_flow():
    ex = executive.build_executive(_exec_feed())
    cco = ex["cco"]
    rows = cco["panels"]["enforcement"]
    assert [r["severity"] for r in rows] == ["critical", "high", "high"]
    assert rows[0]["authority"] == "BCB" and rows[0]["label"].startswith("Banvox")
    assert all(k in rows[0] for k in ("kind_label", "confidence", "sources", "title", "industries"))
    allb, bank, bet = (cco["by_industry"][k] for k in ("__all__", "banking", "betting"))
    assert allb["n_enforcement"] == 3 and allb["enforcement_severity"] == "critical"
    assert bank["n_enforcement"] == 3  # market-wide (untagged) + the Itaú CNEP row
    assert bet["n_enforcement"] == 2 and bet["n_enforcement_severe"] == 2
    assert any(r["kind"] == "enforcement" for r in cco["panels"]["risk_register"])
    assert any(r.get("entity") == "itau" and r["action"] == "flag_entity" for r in cco["panels"]["recommendations"])
    flow = [t for t in ex["flow"] if t["trigger"] == "enforcement"]
    assert len(flow) == 1 and flow[0]["officer"] == "cco" and flow[0]["action_ref"] == "open_watch"


def test_ask_cards_cite_the_action_sources():
    from src.dashboard import agent_ask

    cards = agent_ask.enforcement_cards(_exec_feed())
    liq = next(c for c in cards if c["id"].startswith("enf:liquidacao"))
    assert "liquidação extrajudicial" in liq["narrative"] and "Banvox" in liq["narrative"]
    assert liq["citations"][0]["url"].startswith("https://www.bcb.gov.br")
    assert liq["industries"] == []  # unbound + untagged → fails closed for a scoped tenant
    cnep = next(c for c in cards if c.get("entity") == "itau")
    assert cnep["industries"] == ["banking"] and "integridade" in cnep["lenses"]


def test_feed_projection_scopes_enforcement():
    from src.dashboard import feed_builder

    feed = _exec_feed()
    feed.update({"entities": [], "industries": []})
    out = feed_builder.scope_feed_to_modules(feed, ["betting"])
    assert all(a.get("entity") != "itau" for a in out["enforcement"])
    assert any("Banvox" in (a.get("target") or "") for a in out["enforcement"])
