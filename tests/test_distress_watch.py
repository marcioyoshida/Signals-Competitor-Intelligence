"""Distress coverage (#354–#359) — the Grupo Euro17 miss, replayed offline.

Grupo Euro17 filed for recuperação judicial (TJSP, ~2026-10-16). Onça stayed silent:
the BCB-authorised SCD (cnpj root 59855096) was parked in the review queue by the
prominence gate, the NeoFeed headline named no tracked entity, and the distress store
dropped it. These tests pin every link of that chain."""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.ingest import bcb_autorizacoes, bcb_correspondentes, trade_press
from src.synth import distress, distress_watch as w
from src.synth import entity_registry as er
from src.synth import executive
from test_entity_registry import FakeTable

TODAY = dt.date(2026, 10, 23)
EURO_TITLE = "Com dívida de R$ 1,72 bilhão, Grupo Euro17 pede recuperação judicial"
EURO_SCD = {"name": "EURO17 SOCIEDADE DE CRÉDITO DIRETO S.A.", "cnpj": "59855096",
            "license_class": "Crédito Direto (SCD)", "registry": "SedesSociedades"}
MW = {"name": "MW INSTITUIÇÃO DE PAGAMENTO LTDA.", "cnpj": "11222333",
      "license_class": "Instituição de Pagamento", "registry": "SedesSociedades"}
ENTITIES = [
    {"entity_id": "daycoval", "display_name": "Banco Daycoval", "cnpj_roots": ["62232889"]},
    {"entity_id": "votorantim", "display_name": "Votorantim", "cnpj_roots": ["59588111"],
     "news_search": False},
]
CORR_ROWS = [  # live shape, BCB Informes_Correspondentes 2026-11-05
    {"CnpjContratante": "59588111", "NomeContratante": "BANCO VOTORANTIM S.A.",
     "CnpjCorrespondente": "28924327",
     "NomeCorrespondente": "EURO17 EMPRESARIAL GESTAO DE NEGOCIOS & INVESTIMENTOS LTDA.",
     "Posicao": "04/11/2026"},
    {"CnpjContratante": "62232889", "NomeContratante": "BANCO DAYCOVAL S.A.",
     "CnpjCorrespondente": "49250480", "NomeCorrespondente": "EURO17 PROMOTORA LTDA",
     "Posicao": "04/11/2026"},
    {"CnpjContratante": "99999999", "NomeContratante": "BANCO NAO ACOMPANHADO",
     "CnpjCorrespondente": "1", "NomeCorrespondente": "NEUROEURO17X LTDA"},  # loose contains()
]


def _item(title, i="news:1", url="https://neofeed.com.br/x", date="2026-10-22", src="News"):
    return {"id": i, "title": title, "url": url, "date": date, "source": src}


# --- #354 classifier + sweep ----------------------------------------------------------------

def test_classifier_knows_the_bcb_resolution_regimes():
    c = distress.classify_distress
    assert c("BC decreta liquidação extrajudicial da MW Instituição de Pagamento") == "liquidacao_extrajudicial"
    assert c("Banco Central decreta intervenção no Banco X") == "intervencao"
    assert c("BC impõe RAET a banco") == "raet"
    assert c(EURO_TITLE) == "recuperacao_judicial"
    # the everyday senses stay out
    assert c("Loja faz liquidação de estoque") is None
    assert c("Liquidação financeira de operações no Pix bate recorde") is None
    assert c("Intervenção federal no Rio") is None


def test_sweep_keeps_untracked_distress_headlines_and_dedups_known():
    outlet = [
        {"id": "news:a", "title": EURO_TITLE, "url": "https://neofeed.com.br/a",
         "date": "2026-10-22", "source": "News", "publisher": "NeoFeed", "raw": "x" * 500,
         "company": "pede recuperação judicial"},
        {"id": "news:b", "title": "Nubank lança cartão", "date": "2026-10-22"},
        {"id": "news:c", "title": "Banco Master pede recuperação judicial", "date": "2026-10-22"},
        {"id": "news:d", "title": "Antiga falência", "date": "2026-09-01"},  # outside 30-day lookback
        {"id": "news:e", "title": "Banco Y decreta falência", "date": "2026-10-01"},  # inside it
    ]
    known = [{"id": "news:c", "title": "Banco Master pede recuperação judicial"}]
    out = trade_press.distress_sweep(outlet, known=known, today=TODAY)
    assert [r["id"] for r in out] == ["news:a", "news:e"]
    assert out[0]["query_kind"] == "distress" and out[0]["distress_kind"] == "recuperacao_judicial"
    assert "raw" not in out[0]  # compact: the news task result shares SFN's 256 KB
    assert "company" not in out[0] and "name" not in out[0]  # query text is not an entity


def test_fetch_distress_queries_parses_each_query():
    rss = b"""<rss><channel><item><title>Grupo Euro17 pede recuperacao judicial - NeoFeed</title>
      <link>https://news.google.com/x</link><pubDate>Thu, 22 Oct 2026 10:00:00 GMT</pubDate>
      <source url="https://neofeed.com.br">NeoFeed</source></item></channel></rss>"""
    seen = []
    out = trade_press.fetch_distress_queries(["q1", "q2"], fetcher=lambda q: seen.append(q) or rss,
                                             pause_sec=0)
    assert seen == ["q1", "q2"] and len(out) == 2


# --- #355 the wider resolution ---------------------------------------------------------------

def test_unresolved_sink_collects_guard_passing_headlines_only():
    items = [_item(EURO_TITLE),
             _item("Cliente do Banco X pede recuperação judicial", "news:2"),  # creditor framing
             _item("Banco Master pede recuperação judicial", "news:3")]
    sink = []
    ev = distress.detect_distress_events(
        items, resolver=lambda it: ["banco_master"] if "Master" in it["title"] else [],
        today=TODAY, unresolved=sink)
    assert [e["entity"] for e in ev] == ["banco_master"]
    assert [i["id"] for i in sink] == ["news:1"]


def test_match_headline_roles_and_precision():
    idx = w.build_wide_index([EURO_SCD, MW,
                              {"name": "SICOOB CREDI A", "cnpj": "1"}, {"name": "SICOOB CREDI B", "cnpj": "2"},
                              {"name": "BANCO NOVO DIGITAL S.A.", "cnpj": "3"}])
    m = lambda t: [(c["cnpj"], r) for c, r in w.match_headline(t, idx)]
    assert m(EURO_TITLE) == [("59855096", "group")]            # a GROUP fact, not the SCD's own RJ
    assert m("Euro17 SCD pede recuperação judicial") == [("59855096", "self")]
    assert m("BC decreta liquidação extrajudicial da MW Instituição de Pagamento Ltda.") == [("11222333", "self")]
    assert m("Justiça decreta falência da Euro17") == [("59855096", "group")]
    assert m("Sicoob pede recuperação judicial") == []          # generic head
    assert m("Novo banco pede recuperação judicial") == []      # generic head
    assert m("Recuperação judicial abala o mercado; Euro17 comenta") == []  # not the subject


def test_ambiguous_brand_attributes_to_no_one():
    rows = [{"name": f"ACMEX {s} LTDA", "cnpj": str(10000000 + i)} for i, s in enumerate("ABCD")]
    idx = w.build_wide_index(rows)
    assert w.match_headline("Acmex pede recuperação judicial", idx) == []


def _review(table, cnpj="59855096"):
    prof = {"entity_id": "euro17", "display_name": "Euro17", "auto_ok": True,
            "raw_name": EURO_SCD["name"], "aliases": [EURO_SCD["name"], "Euro17"],
            "cnpj_roots": [cnpj], "industries": ["fintech"], "license_class": "Crédito Direto (SCD)",
            "confidence": "structured", "source": "bcb_autorizacoes"}  # the live profile shape
    er.propose_review("discovery", key="euro17", proposed="Euro17", reason="prominence_gate",
                      confidence="structured",
                      payload={"profile": prof, "source": "bcb_autorizacoes", "cnpj": cnpj}, table=table)
    return "discovery:euro17"


def test_euro17_end_to_end_promotes_links_correspondents_and_surfaces():
    """The 2026-10 miss, replayed: the parked SCD is promoted (distress overrides the
    prominence gate), the RJ is recorded as a GROUP fact, and both contracting banks get a
    correspondent row their compliance officers see."""
    t = FakeTable()
    for e in ENTITIES:
        er.put_entity(e["entity_id"], e["display_name"], [e["display_name"]],
                      cnpj_roots=e["cnpj_roots"], news_search=e.get("news_search", True), table=t)
    rid = _review(t)
    out = w.run([_item(EURO_TITLE)], [], {}, entities=ENTITIES, bcb_rows=[EURO_SCD],
                reviews_fn=lambda: er.list_reviews(status="pending", table=t),
                promote_fn=w.registry_promote_fn(table=t),
                correspondent_lookup=lambda brand: CORR_ROWS if brand == "EURO17" else [],
                qsa={}, exits=[], signal_fn=lambda r, s: er.record_review_signal(r, s, table=t),
                today=TODAY)
    assert out["summary"]["promoted"] == ["euro17"]
    assert er.get_entity("euro17", table=t)["cnpj_roots"] == ["59855096"]   # #359 creates it
    rv = t.get_item(Key={"pk": f"REVIEW#{rid}"})["Item"]
    assert rv["status"] == "approved" and rv["decided_by"] == "distress_override"
    assert rv["signals"][0]["kind"] == "distress"
    [ev] = out["tracked"]
    assert (ev["entity"], ev["role"], ev["label"]) == (
        "euro17", "group", "Grupo econômico em recuperação judicial")
    assert [c["entity"] for c in ev["correspondent_of"]] == ["daycoval", "votorantim"]
    idx = distress.merge_distress({}, out["tracked"], today=TODAY, untracked=out["untracked"])
    assert list(idx["records"]) == ["euro17#group:recuperacao_judicial"]
    rows = distress.list_records(idx)
    by = {(r["entity"], r["role"]) for r in rows}
    assert by == {("euro17", "group"), ("daycoval", "correspondent"), ("votorantim", "correspondent")}
    corr = next(r for r in rows if r["entity"] == "daycoval")
    assert corr["correspondent"] == "EURO17 PROMOTORA LTDA" and corr["id"].endswith(":corr:daycoval")
    assert corr["label"] == "Correspondente em recuperação judicial"
    assert corr["date"] == "2026-10-22"  # push fires on the first sighting


def test_untracked_when_no_promotion_possible_and_no_rescan():
    idx_in: dict = {}
    calls = []
    out = w.run([_item("BC decreta liquidação extrajudicial da MW Instituição de Pagamento Ltda.")],
                [], idx_in, entities=ENTITIES, bcb_rows=[MW], reviews_fn=lambda: [],
                promote_fn=None, correspondent_lookup=lambda b: calls.append(b) or [],
                qsa={}, exits=[], today=TODAY)
    [u] = out["untracked"]
    assert (u["entity"], u["cnpj"], u["role"], u["kind"]) == (None, "11222333", "self",
                                                                "liquidacao_extrajudicial")
    assert calls == []  # "mw" is not a distinctive brand → no correspondent fishing
    merged = distress.merge_distress({}, [], today=TODAY, untracked=out["untracked"])
    assert list(merged["untracked"]) == ["cnpj:11222333#liquidacao_extrajudicial"]
    [row] = distress.list_records(merged)
    assert row["tracked"] is False and row["entity"] is None


def test_correspondent_lookup_runs_once_per_record():
    ev = [{"entity": None, "kind": "recuperacao_judicial", "cnpj": "59855096", "brand": "euro17"}]
    prev = {"cnpj:59855096#recuperacao_judicial": {"correspondent_checked": "2026-10-22",
                                                   "correspondent_of": [{"entity": "daycoval"}]}}
    calls = []
    w.attach_correspondents(ev, {}, lookup=lambda b: calls.append(b) or [], already=prev)
    assert calls == [] and ev[0]["correspondent_of"] == [{"entity": "daycoval"}]


def test_correspondents_client_builds_a_bounded_filter():
    seen = {}

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"value": CORR_ROWS[:2] + CORR_ROWS[:1]}

    rows = bcb_correspondentes.lookup_by_name("euro17'; drop", get=lambda u, params, timeout: seen.update(params) or R())
    assert seen["$filter"] == "contains(NomeCorrespondente,'EURO17 DROP')"
    assert len(rows) == 2  # one row per (contratante, correspondente)
    assert bcb_correspondentes.lookup_by_name("mw") == []


# --- #356 controller siblings ----------------------------------------------------------------

def test_controller_siblings_share_a_pf_socio_not_a_director():
    qsa = {"entities": {
        "a": {"socios": [{"name": "FULANO DE TAL", "doc_mask": "***123456**", "qual": "Sócio-Administrador"}]},
        "b": {"socios": [{"name": "FULANO DE TAL", "doc_mask": "***123456**", "qual": "Sócio"}]},
        "c": {"socios": [{"name": "FULANO DE TAL", "doc_mask": "***123456**", "qual": "Diretor"}]},
    }}
    own = [{"entity": "a", "kind": "recuperacao_judicial", "label": "Recuperação Judicial", "role": "self",
            "date": "2026-10-22", "title": "t"}]
    [g] = w.controller_siblings(own, qsa)
    assert (g["entity"], g["role"], g["group_of"]) == ("b", "group", "a")
    assert w.controller_siblings([{**own[0], "role": "group"}], qsa) == []


def test_entity_status_prefers_own_record_over_group():
    idx = {"records": {
        "x#group:falencia": {"entity": "x", "kind": "falencia", "role": "group"},
        "x#recuperacao_extrajudicial": {"entity": "x", "kind": "recuperacao_extrajudicial"}}}
    assert distress.entity_status(idx, "x")["kind"] == "recuperacao_extrajudicial"


def test_executive_trusts_graded_confidence_and_keeps_third_party_off_the_tile():
    feed = {"distress": [
        {"entity": "a", "kind": "falencia", "confidence": "regulatory"},
        {"entity": "b", "kind": "falencia", "confidence": "reported"},
        {"entity": "c", "kind": "falencia", "confidence": "corroborated", "role": "correspondent"},
        {"entity": None, "kind": "falencia", "confidence": "regulatory"}]}
    assert [d["entity"] for d in executive._trusted_distress(feed)] == ["a"]
    assert [d["entity"] for d in executive._third_party_distress(feed)] == ["c"]


# --- #358 register exits ---------------------------------------------------------------------

def _recs(rows, res="SedesSociedades"):
    return [{"cnpj": r[0], "name": r[1], "license_class": "SCD", "registry": res} for r in rows]


def test_register_exit_diff_and_shrink_guard():
    base = [(f"{10000000 + i}", f"INST {i}") for i in range(100)]
    prev = bcb_autorizacoes.snapshot(_recs(base), "2026-11-04")
    cur = bcb_autorizacoes.snapshot(_recs(base[1:]), "2026-11-05")
    exits, skipped = bcb_autorizacoes.diff_exits(prev, cur)
    assert [x["cnpj"] for x in exits] == ["10000000"] and skipped == []
    partial = bcb_autorizacoes.snapshot(_recs(base[:50]), "2026-11-05")
    assert bcb_autorizacoes.diff_exits(prev, partial) == ([], ["SedesSociedades"])
    assert bcb_autorizacoes.diff_exits({}, cur) == ([], [])


class FakeS3:
    def __init__(self):
        self.objs = {}

    def get_object(self, Bucket, Key):
        if Key not in self.objs:
            raise KeyError(Key)

        class B:
            def __init__(s, b):
                s.b = b

            def read(s):
                return s.b
        return {"Body": B(self.objs[Key])}

    def put_object(self, Bucket, Key, Body, **kw):
        self.objs[Key] = Body


def test_update_register_state_logs_and_clears_exits():
    s3 = FakeS3()
    base = _recs([("59855096", "EURO17 SCD"), ("11111111", "OUTRA")] + [(f"{20000000 + i}", "X") for i in range(40)])
    bcb_autorizacoes.update_register_state(base, "b", s3=s3, as_of="2026-11-04")
    r = bcb_autorizacoes.update_register_state(base[1:], "b", s3=s3, as_of="2026-11-05")
    assert r["new_exits"] == 1
    log = json.loads(s3.objs[bcb_autorizacoes.EXITS_KEY])
    assert [x["cnpj"] for x in log["exits"]] == ["59855096"]
    bcb_autorizacoes.update_register_state(base, "b", s3=s3, as_of="2026-11-06")  # it came back
    assert json.loads(s3.objs[bcb_autorizacoes.EXITS_KEY])["exits"] == []


def test_exit_events_are_regulatory_facts_and_idempotent():
    evs = w.exit_events([{"cnpj": "62232889", "name": "BANCO DAYCOVAL S.A.", "date": "2026-11-05"},
                         {"cnpj": "11222333", "name": "MW IP", "date": "2026-11-05"}],
                        {"62232889": "daycoval"})
    assert [e["entity"] for e in evs] == ["daycoval", None]
    assert {e["kind"] for e in evs} == {"saida_cadastro_bcb"}
    idx = distress.merge_distress({}, [evs[0]], today=TODAY)
    idx = distress.merge_distress(idx, [evs[0]], today=TODAY)
    rec = idx["records"]["daycoval#saida_cadastro_bcb"]
    assert rec["confidence"] == "regulatory" and rec["mentions"] == 1


# --- #359 review queue -----------------------------------------------------------------------

def test_approving_a_discovery_review_creates_the_entity():
    t = FakeTable()
    rid = _review(t)
    er.resolve_review(rid, "approved", table=t)
    ent = er.get_entity("euro17", table=t)
    assert ent and ent["cnpj_roots"] == ["59855096"] and ent.get("news_search") is False
    assert er.resolve_by_cnpj("59855096", table=t) == "euro17"


def test_approving_a_news_only_discovery_creates_a_curated_entity():
    t = FakeTable()
    er.propose_review("discovery", key="EuroPrime", proposed="EuroPrime",
                      reason="news_keyword_harvest:ner", payload={"surface": "EuroPrime"}, table=t)
    er.resolve_review("discovery:europrime", "approved", table=t)
    assert er.get_entity("europrime", table=t)["display_name"] == "EuroPrime"


def test_reviews_rank_by_fresh_signal_then_age_and_flag_stale():
    t = FakeTable()
    for k, when in (("old", "2026-08-01T00:00:00+00:00"), ("mid", "2026-10-01T00:00:00+00:00"),
                    ("hot", "2026-10-20T00:00:00+00:00")):
        er.propose_review("discovery", key=k, proposed=k, table=t)
        t.items[f"REVIEW#discovery:{k}"]["created_at"] = when
    assert er.record_review_signal("discovery:hot", {"kind": "distress", "ref": "u", "date": "2026-10-22"}, table=t)
    assert not er.record_review_signal("discovery:hot", {"kind": "distress", "ref": "u", "date": "2026-10-22"}, table=t)
    ranked = er.rank_reviews(er.list_reviews(table=t), today=TODAY)
    assert [r["review_id"] for r in ranked] == ["discovery:hot", "discovery:old", "discovery:mid"]
    assert ranked[0]["signal_score"] > 9 and ranked[1]["stale"] is True and ranked[2]["stale"] is False


def test_update_from_digest_reads_swept_items_and_runs_the_watch():
    s3 = FakeS3()
    digest = {"news": {"items": [], "context": [],
                       "distress_items": [_item(EURO_TITLE), _item(EURO_TITLE)]}}  # dup dropped
    seen = {}

    def watch(unresolved, own, index):
        seen["n"] = len(unresolved)
        return w.run(unresolved, own, index, entities=ENTITIES, bcb_rows=[EURO_SCD],
                     reviews_fn=lambda: [], promote_fn=None, correspondent_lookup=lambda b: CORR_ROWS,
                     qsa={}, exits=[], today=TODAY)

    out = distress.update_from_digest(digest, "b", resolver=lambda it: [], s3=s3, today=TODAY, watch=watch)
    assert seen["n"] == 1 and out["untracked"] == 1 and out["watch"]["correspondent_links"] == 1
    idx = json.loads(s3.objs[distress.INDEX_KEY])
    rec = idx["untracked"]["cnpj:59855096#recuperacao_judicial"]
    assert rec["role"] == "group" and rec["matched_via"] == "bcb_register"
    rows = distress.list_records(idx)
    assert {r["entity"] for r in rows} == {None, "daycoval", "votorantim"}


# --- live false positives, 2026-11-06 (first live run) -----------------------------------------

def _noisy_corr(head):
    # a common word: many unrelated correspondents carry it
    return [{"CnpjContratante": "00000000", "CnpjCorrespondente": str(i),
             "NomeCorrespondente": f"EMPRESA {i} {head} LTDA"} for i in range(9)]


def test_mid_phrase_and_genitive_modifier_heads_do_not_bind():
    idx = w.build_wide_index([{"name": "ENERGY PAGAMENTOS S.A.", "cnpj": "36969658",
                               "license_class": "Instituição de Pagamento"},
                              {"name": "NORDESTE CREDITO SCD S.A.", "cnpj": "03773153",
                               "license_class": "Crédito Direto (SCD)"}])
    assert w.match_headline("Aeris Energy recorre a recuperação extrajudicial de R$ 1,5 bi", idx) == []
    assert w.match_headline("FÁBRICA DE PÁS EÓLICAS DO NORDESTE PEDE RECUPERAÇÃO JUDICIAL", idx) == []
    # the true shapes still bind
    assert w.match_headline("Energy pede recuperação judicial", idx)
    assert w.match_headline("EXCLUSIVO: Com dívida de R$ 1,72 bilhão, Grupo Nordeste pede recuperação judicial", idx)


def test_receita_and_fund_candidates_never_head_match_or_promote():
    reviews = [{"kind": "discovery", "status": "pending", "review_id": "discovery:receita_03773153",
                "proposed": "NORDESTE SAUDE EMPRESARIAL",
                "payload": {"cnpj": "03773153", "source": "receita_bulk",
                            "profile": {"cnae": "6550200", "source": "receita_bulk"}}}]
    ents = [{"entity_id": "energy-resort", "display_name": "Energy Resort", "news_search": False,
             "industries": ["real-estate-funds"], "cnpj_roots": ["36969658"]}]
    idx = w.build_wide_index([], reviews, ents)
    assert idx["heads"] == {}
    assert w.match_headline("Nordeste pede recuperação judicial", idx) == []
    t = FakeTable()
    t.put_item(Item={"pk": "REVIEW#discovery:receita_03773153", "status": "pending", "kind": "discovery",
                     "review_id": "discovery:receita_03773153", "payload": reviews[0]["payload"]})
    fn = w.registry_promote_fn(table=t)
    assert fn({"review_id": "discovery:receita_03773153",
               "_profile": reviews[0]["payload"]["profile"]}, "k") is None
    assert t.items["REVIEW#discovery:receita_03773153"]["status"] == "pending"  # untouched


def test_common_word_brand_is_dropped_by_the_correspondent_corpus():
    idx = w.build_wide_index([{"name": "ENERGY PAGAMENTOS S.A.", "cnpj": "36969658",
                               "license_class": "Instituição de Pagamento"}])
    out = w.run([_item("Grupo Energy pede recuperação judicial")], [], {}, entities=ENTITIES,
                bcb_rows=[{"name": "ENERGY PAGAMENTOS S.A.", "cnpj": "36969658",
                           "license_class": "Instituição de Pagamento"}],
                reviews_fn=lambda: [], promote_fn=None, correspondent_lookup=_noisy_corr,
                qsa={}, exits=[], today=TODAY)
    assert out["tracked"] == [] and out["untracked"] == []
    # without a way to vet the brand, no group claim is made at all
    out = w.run([_item("Grupo Euro17 pede recuperação judicial")], [], {}, entities=ENTITIES,
                bcb_rows=[EURO_SCD], reviews_fn=lambda: [], promote_fn=None,
                correspondent_lookup=None, qsa={}, exits=[], today=TODAY)
    assert out["untracked"] == []
