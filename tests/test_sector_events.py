"""#177 — industry-level regulatory events, tested on the REAL 2026-09-25/26 inputs (incident
#173: MP 1.394 banned online betting and Onça's CRO/Ask showed nothing)."""
import copy
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import sector_events as se

FIX = Path(__file__).resolve().parent / "fixtures" / "sector_events"


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


RUNS = _load("news_runs_2026-09-25_26.json")["runs"]
DOU = _load("dou_2026-09-25.json")
ENTITIES = _load("entities.json")["entities"]
COVERED = {i for e in ENTITIES for i in (e.get("industries") or [])}


def _run_day(run):
    return dt.date.fromisoformat(run["run"][:10])


def replay(*, with_dou=False, dou_run=0, store=None):
    """Feed the six real synth-time digests through build_events, run by run. ``with_dou``
    adds the real MP 1.394 DOU items (data-contract fields from #174/#175) to run ``dou_run``'s
    structured digest."""
    store = store or {}
    reports = []
    for i, run in enumerate(RUNS):
        digest = {"news": copy.deepcopy(run["news"])}
        if with_dou and i == dou_run:
            digest["dou"] = copy.deepcopy(DOU["dou"])
        off, news = se.split_digest(digest)
        store, rep = se.build_events(store, off, news, today=_run_day(run), industries=COVERED)
        reports.append(rep)
    return store, reports


# --- headline classification on the real headlines ------------------------------------------
POSITIVE = [
    ("Como a proibição das bets no Brasil pode impactar as ações da B3?", "B3"),
    ("Como o governo Lula aprovou a lei das bets, votou contra proibição de apostas a inscritos no "
     "Serasa e SPC e CadÚnico até proibi-las agora", "Serasa"),
    ("Bets estão proibidas, mas jogatina continua pelas loterias da Caixa Econômica Federal", "Caixa Econômica"),
    ("Governo suspende 14 casas de apostas, entre elas marcas da Pixbet", "Pixbet"),
]
NEGATIVE = [
    ("PESQUISA ATLAS/BLOOMBERG: 75% defendem proibição das bets e governo Lula é apontado como "
     "principal responsável pela expansão", "Bloomberg", "poll"),
    ("Pesquisa Atlas/Bloomberg aponta que 75% dos brasileiros são a favor da proibição das apostas online",
     "Bloomberg", "poll"),
    ("MTST protesta na porta da Betano, em São Paulo, e cobra proibição das apostas online", "Betano", "demand"),
    ("Fluminense: Superbet notifica o clube sobre rescisão contratual caso apostas sejam proibidas no Brasil",
     "Superbet", "hypothetical"),
    ("Justiça bloqueia R$ 1 bilhão, mas nega suspensão de apostas na Pixbet", "Pixbet", "negated"),
    ("Justiça da Paraíba determina suspensão de plataformas de apostas da Pixbet em todo o país",
     "Pixbet", "operator_specific"),
    ("Coinbase suspende oito pares de negociação de criptomoedas", "Coinbase", "operator_specific"),
]


@pytest.mark.parametrize("title,company", POSITIVE)
def test_real_ban_headlines_are_sector_reports(title, company):
    a = se.assess_headline(title, company=company)
    assert a["verdict"] == "report", a
    assert "betting" in a["industries"]


@pytest.mark.parametrize("title,company,reason", NEGATIVE)
def test_real_negatives_are_rejected_with_reason(title, company, reason):
    a = se.assess_headline(title, company=company)
    assert a["verdict"] == "rejected"
    assert a["reason"] == reason


def test_operator_promo_and_single_word_bet_are_irrelevant():
    # "saída da bet" names no SECTOR and carries no change vocabulary.
    assert se.assess_headline("Flamengo e Betano: qual pode ser o prejuízo do clube com possível "
                              "saída da bet?", company="Betano")["verdict"] == "irrelevant"
    assert se.assess_headline("Betano eSports: como apostar em CS2, Valorant, LoL e Dota",
                              company="Betano")["verdict"] == "irrelevant"


def test_sector_query_item_uses_its_tag_even_without_a_sector_noun():
    a = se.assess_headline("Governo publica medida provisória e setor reage", industries=["betting"])
    assert a["verdict"] == "report" and a["industries"] == ["betting"]


def test_instrument_refs_normalised():
    assert se.instrument_refs("MEDIDA PROVISÓRIA Nº 1.394, DE 25 DE SETEMBRO DE 2026") == ["mp-1394"]
    assert "lei-14790" in se.instrument_refs("altera a Lei nº 14.790, de 29 de dezembro de 2023")


def test_publisher_key_collapses_name_and_domain():
    assert se.publisher_key({"publisher": "Seu Dinheiro"}) == se.publisher_key({"publisher": "seudinheiro.com"})
    assert se.publisher_key({"publisher": "O Dia"}) == se.publisher_key({"publisher": "odia.ig.com.br"})


# --- replay: pre-fix inputs (news only) ---------------------------------------------------------
def test_prefix_news_replay_creates_one_corroborated_betting_event():
    store, reports = replay()
    evs = store["events"]
    assert len(evs) == 1
    ev = evs[0]
    assert ev["industry"] == "betting"
    assert ev["confidence"] == "corroborated" and ev["n_official"] == 0
    assert ev["change_type"] == "ban" and ev["severity"] == "high"  # press alone never "critical"
    assert set(ev["outlets"]) == {"InfoMoney", "JC", "Maranhão Hoje"}
    titles = " | ".join(s["title"] for s in ev["sources"])
    assert "75%" not in titles and "MTST" not in titles  # poll / protest never cited
    assert all(s["url"] for s in ev["sources"])  # content honesty: every source is a real link
    # first run with only a single outlet: no event yet (pending), not a guess
    assert not reports[3]["created"] and store["pending"] == []


def test_event_id_is_stable_across_runs():
    store, _ = replay()
    ev_id = store["events"][0]["id"]
    again, _ = replay(store=copy.deepcopy(store))
    assert [e["id"] for e in again["events"]] == [ev_id]
    assert len(again["events"][0]["sources"]) == len(store["events"][0]["sources"])  # no dup sources


def test_single_outlet_is_pending_not_an_event():
    day = dt.date(2026, 9, 26)
    item = {"id": "n1", "title": "Bets estão proibidas no Brasil", "publisher": "A", "date": "2026-09-26",
            "url": "https://a.example/1"}
    store, rep = se.build_events({}, [], [item], today=day)
    assert store["events"] == [] and len(store["pending"]) == 1
    # the same outlet again (syndication) still is ONE outlet
    dup = {**item, "id": "n2", "url": "https://a.example/2"}
    store, _ = se.build_events(store, [], [dup], today=day)
    assert store["events"] == []
    # a second independent outlet two runs later corroborates it
    other = {**item, "id": "n3", "publisher": "B", "url": "https://b.example/3"}
    store, rep = se.build_events(store, [], [other], today=day + dt.timedelta(days=1))
    assert len(store["events"]) == 1 and store["pending"] == []


def test_pending_expires():
    item = {"id": "n1", "title": "Bets estão proibidas no Brasil", "publisher": "A", "date": "2026-09-01",
            "url": "https://a.example/1"}
    store, _ = se.build_events({}, [], [item], today=dt.date(2026, 9, 1))
    store, _ = se.build_events(store, [], [], today=dt.date(2026, 9, 20))
    assert store["pending"] == []


# --- replay: post-fix inputs (the DOU act lands) -----------------------------------------------
def test_replay_with_dou_produces_critical_betting_event_citing_mp_1394():
    store, _ = replay(with_dou=True)
    evs = store["events"]
    assert len(evs) == 1
    ev = evs[0]
    assert ev["industry"] == "betting" and ev["severity"] == "critical"
    assert ev["confidence"] == "official"
    lead = ev["sources"][0]
    assert lead["kind"] == "official"
    assert lead["title"].startswith("MEDIDA PROVISÓRIA Nº 1.394")
    assert lead["url"] == ("https://www.in.gov.br/web/dou/-/"
                           "medida-provisoria-n-1.394-de-25-de-setembro-de-2026-734808521")
    assert lead["section"] == "DO1_EXTRA_A"
    assert ev["summary"].startswith("Proíbe a exploração, a oferta, a intermediação e a publicidade")
    assert "intermediaçãoíbe" not in ev["summary"]  # search-snippet splice removed, nothing added
    # the Presidência despacho forwarding the MP joins as a secondary source, not a new event
    kinds = [s["kind"] for s in ev["sources"]]
    assert kinds.count("official") == 2 and kinds.index("news") > 1
    assert ev["n_outlets"] == 3
    assert ev["id"] == "sector_event:betting:mp-1394"


def test_official_act_upgrades_an_existing_press_event_keeping_its_id():
    store, _ = replay()
    press_id = store["events"][0]["id"]
    off, _ = se.split_digest({"dou": copy.deepcopy(DOU["dou"])})
    store, rep = se.build_events(store, off, [], today=dt.date(2026, 9, 27), industries=COVERED)
    assert [e["id"] for e in store["events"]] == [press_id]
    ev = store["events"][0]
    assert ev["severity"] == "critical" and ev["confidence"] == "official"
    assert ev["sources"][0]["title"].startswith("MEDIDA PROVISÓRIA Nº 1.394")


def test_low_severity_and_orphan_secondary_acts_do_not_create_events():
    base = {"source": "DOU", "kind": "regulatory", "industries": ["betting"], "date": "2026-09-25",
            "url": "https://www.in.gov.br/web/dou/-/x"}
    low = {**base, "id": "dou:a", "title": "PORTARIA SPA/MF Nº 1, DE 2026", "severity": "low"}
    edital = {**base, "id": "dou:b", "title": "EDITAL DE CITAÇÃO", "doc_type": "Edital", "severity": "medium"}
    store, _ = se.build_events({}, [low, edital], [], today=dt.date(2026, 9, 25))
    assert store["events"] == []


def test_unknown_severity_treated_as_medium_and_uncovered_industry_ignored():
    it = {"id": "dou:c", "source": "DOU", "kind": "regulatory", "industries": ["betting", "space-mining"],
          "title": "DECRETO Nº 12.000, DE 2026", "date": "2026-09-25", "url": "https://x/1"}
    store, _ = se.build_events({}, [it], [], today=dt.date(2026, 9, 25), industries={"betting"})
    assert [(e["industry"], e["severity"]) for e in store["events"]] == [("betting", "medium")]


def test_split_digest_finds_acts_in_any_section_and_sector_news_anywhere():
    digest = {
        "federal_acts": {"items": [{"id": "fa:1", "source": "Planalto", "kind": "regulatory",
                                    "industries": ["betting"], "title": "LEI Nº 1"}]},
        "dou": {"items": [{"id": "dou:noind", "source": "DOU", "kind": "regulatory", "title": "x"}]},
        "news": {"items": [{"id": "n:1", "title": "a"}], "context": [{"id": "n:1", "title": "a"}]},
        "sector_news": {"items": [{"id": "s:1", "query_kind": "sector", "industries": ["betting"]}]},
        "gdelt_macro": [{"id": "g"}],
    }
    off, news = se.split_digest(digest)
    assert [o["id"] for o in off] == ["fa:1"]  # an act WITHOUT industries is not a sector act
    assert sorted(n["id"] for n in news) == ["n:1", "s:1"]


# --- affected entities, feed projection, scoping, S3 orchestrator ------------------------------
def test_affected_entities_are_the_whole_active_betting_roster():
    store, _ = replay(with_dou=True)
    rosters = se.industry_rosters(ENTITIES)
    ev = se.with_affected(store["events"], rosters)[0]
    assert ev["n_affected"] == 83
    assert "betano" in ev["entities"] and "b3" not in ev["entities"]


def test_for_feed_uses_entity_attrs_and_window():
    store, _ = replay(with_dou=True)
    attrs = {e["entity_id"]: {"label": e["display_name"], "industries": e["industries"]} for e in ENTITIES}
    rows = se.for_feed(store, entity_attrs=attrs, today=dt.date(2026, 9, 27))
    assert rows[0]["n_affected"] == 83 and "affected_entities" not in rows[0]
    assert se.for_feed(store, entity_attrs=attrs, today=dt.date(2027, 1, 30)) == []


def test_scope_by_industry():
    store, _ = replay(with_dou=True)
    assert se.scope(store["events"], ["betting"]) and not se.scope(store["events"], ["banking"])


class FakeS3:
    def __init__(self):
        self.store = {}

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


def test_update_from_digest_persists_and_accumulates():
    s3 = FakeS3()
    for i, run in enumerate(RUNS):
        digest = {"news": run["news"], **({"dou": DOU["dou"]} if i == 0 else {})}
        out = se.update_from_digest(digest, "b", entities=ENTITIES, s3=s3, today=_run_day(run))
    saved = json.loads(s3.store[se.STORE_KEY])
    assert out["events"] == 1 and saved["events"][0]["severity"] == "critical"
    assert saved["events"][0]["n_affected"] == 83


def test_only_sector_wide_acts_open_events_under_175_severity():
    """#175 marks every framework amendment (most BCB/CVM resoluções) and operator-level bans
    as "high"; those must not flood the sector-event panel."""
    base = {"source": "DOU", "kind": "regulatory", "date": "2026-09-20", "url": "https://x/"}
    bcb = {**base, "id": "bcb:1", "source": "BCB", "industries": ["banking"], "severity": "high",
           "doc_type": "Resolução BCB", "title": "RESOLUÇÃO BCB Nº 588", "severity_reason": "amends a framework"}
    operator = {**base, "id": "dou:op", "industries": ["betting"], "severity": "high",
                "doc_type": "Portaria", "title": "PORTARIA SPA/MF Nº 9, DE 2026",
                "severity_reason": "operator-level ban/suspension/revocation: 'suspende'"}
    lei = {**base, "id": "dou:lei", "industries": ["insurance"], "severity": "high", "doc_type": "Lei",
           "title": "LEI Nº 15.100, DE 20 DE SETEMBRO DE 2026", "severity_reason": "new framework"}
    store, _ = se.build_events({}, [bcb, operator, lei], [], today=dt.date(2026, 9, 20))
    assert [(e["industry"], e["severity"]) for e in store["events"]] == [("insurance", "high")]


def test_high_act_attaches_to_an_existing_sector_event():
    store, _ = replay(with_dou=True)
    impl = {"id": "dou:portaria-spa", "source": "DOU", "kind": "regulatory", "industries": ["betting"],
            "severity": "high", "doc_type": "Portaria",
            "title": "PORTARIA SPA/MF Nº 2.990, DE 26 DE SETEMBRO DE 2026",
            "text": "Regulamenta a Medida Provisória nº 1.394, de 25 de setembro de 2026.",
            "date": "2026-09-26", "url": "https://www.in.gov.br/web/dou/-/x"}
    store, rep = se.build_events(store, [impl], [], today=dt.date(2026, 9, 26))
    ev = store["events"][0]
    assert len(store["events"]) == 1 and ev["n_official"] == 3 and rep["updated"] == [ev["id"]]
    assert ev["sources"][0]["title"].startswith("MEDIDA PROVISÓRIA")  # critical act stays first


def test_entity_news_item_carrying_176_industries_counts_as_tagged():
    it = {"id": "n1", "title": "Governo publica medida provisória e setor reage", "publisher": "A",
          "date": "2026-09-26", "url": "https://a/1", "query_kind": "entity", "company": "B3",
          "industries": ["betting"]}
    store, rep = se.build_events({}, [], [it], today=dt.date(2026, 9, 26))
    assert rep["reports"] and store["pending"][0]["industry"] == "betting"


def test_unrelated_acts_and_headlines_do_not_merge_into_an_event_by_window():
    # Live dry run 2026-09-27: routine SPA editais de citação (09-22) merged into the MP 1.394
    # ban (dating it 09-24), and a CMN FIDC headline merged into MP 1.393 (Desenrola).
    store, _ = replay(with_dou=True)
    edital = {"id": "dou:edital", "source": "DOU", "kind": "regulatory", "industries": ["betting"],
              "severity": "medium", "doc_type": "Edital",
              "title": "EDITAL DE CITAÇÃO DE 22 DE SETEMBRO DE 2026", "text": "Fica citada a empresa X ...",
              "date": "2026-09-22", "url": "https://www.in.gov.br/web/dou/-/edital"}
    desenrola = {"id": "dou:mp-1393", "source": "DOU", "kind": "regulatory", "industries": ["securitization"],
                 "severity": "high", "doc_type": "Medida Provisória",
                 "title": "MEDIDA PROVISÓRIA Nº 1.393, DE 25 DE SETEMBRO DE 2026",
                 "text": "Altera a Lei nº 14.690, de 3 de outubro de 2023, para instituir a Modalidade "
                         "Emergencial de Renegociação de Dívidas - Desenrola Brasil 3.0",
                 "date": "2026-09-25", "url": "https://www.in.gov.br/web/dou/-/mp-1393"}
    fidc = {"id": "n-fidc", "title": "CMN veda a FIDCs aplicação em créditos judiciais", "publisher": "Mattos Filho",
            "date": "2026-09-26", "url": "https://m/1", "query_kind": "sector", "industries": ["securitization"]}
    store, _ = se.build_events(store, [edital, desenrola], [fidc], today=dt.date(2026, 9, 26))
    ban = next(e for e in store["events"] if e["id"].endswith("mp-1394"))
    assert ban["date"] == "2026-09-25" and not any(s.get("id") == "dou:edital" for s in ban["sources"])
    des = next(e for e in store["events"] if e["id"].endswith("mp-1393"))
    assert not any(s.get("id") == "n-fidc" for s in des["sources"])     # a different measure
