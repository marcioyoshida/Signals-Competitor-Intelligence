"""#361–#366 — the Will Bank miss (2026-11-06): official BCB resolution regimes, CNPJ/controller
backfill, status mentions, the lifecycle gate, and register exits kept out of distress."""
from __future__ import annotations

import datetime as dt
import json

from src.ingest import bcb_regimes
from src.synth import distress, distress_watch, executive, lifecycle

TODAY = dt.date(2026, 11, 6)

WILL_DETAIL = {
    "identificador": 100562,
    "Nome": "WILL FINANCEIRA S.A. CRÉDITO, FINANCIAMENTO E INVESTIMENTO",
    "Cnpj": "23862762000100",
    "Segmento": "Sociedade de Crédito, Financiamento e Investimento",
    "Situacao": "Ativo",
    "TipoRegime": "Liquidação Extrajudicial",
    "DataInicio": "2026-01-21T03:00:00Z",
    "DataEncerramento": None,
    "Emails": "liquidacao@willbank.com.br",
    "Site": "https://www.willfinanceira.com.br/",
    "Responsavel": "EFB REGIMES ESPECIAIS DE EMPRESAS LTDA",
    "Paragrafo1": "<p>A instituição teve sua liquidação extrajudicial decretada por meio do "
                  "<a href='https://www.bcb.gov.br/estabilidadefinanceira/exibenormativo?tipo=Ato do "
                  "Presidente&numero=1376'>Ato do Presidente Nº 1376, de 21/01/2026</a>.</p>",
}
MASTER_DETAIL = {
    "identificador": 100540, "Nome": "BANCO MASTER S/A", "Cnpj": "33923798000100",
    "Segmento": "Banco Múltiplo", "Situacao": "Ativo", "TipoRegime": "Liquidação Extrajudicial",
    "DataInicio": "2025-11-18T03:00:00Z", "Emails": "", "Site": "",
    "Paragrafo1": "<p><a href='/estabilidadefinanceira/exibenormativo?tipo=Ato do Presidente&numero=1369'>"
                  "Ato do Presidente Nº 1369, de 18/11/2025</a></p>",
}


def _doc(*details, closed=()):
    rows = [bcb_regimes.normalize(d) for d in details]
    doc = {"active": {r["id"]: r for r in rows}, "closed": {}}
    for d in closed:
        r = bcb_regimes.normalize(d)
        doc["closed"][r["id"]] = {**r, "left_active_on": "2026-11-01"}
    return doc


ENTITIES = [
    {"entity_id": "will_bank", "display_name": "Will Bank", "aliases": ["WILL BANK"], "cnpj_roots": [],
     "controllers": ["Banco Master"]},
    {"entity_id": "banco_master", "display_name": "Banco Master", "aliases": ["BANCO MASTER"],
     "cnpj_roots": []},
    {"entity_id": "nubank", "display_name": "Nubank", "aliases": ["NUBANK"], "cnpj_roots": ["18236120"]},
]


# --- #362 the BCB client -----------------------------------------------------------------------

def test_normalize_parses_the_decree_act_and_kind():
    r = bcb_regimes.normalize(WILL_DETAIL)
    assert r["cnpj"] == "23862762" and r["kind"] == "liquidacao_extrajudicial"
    assert r["start"] == "2026-01-21" and r["act"] == "Ato do Presidente Nº 1376, de 21/01/2026"
    assert r["act_url"].startswith("https://www.bcb.gov.br/estabilidadefinanceira/exibenormativo?")
    assert " " not in r["act_url"]
    m = bcb_regimes.normalize(MASTER_DETAIL)
    assert m["act_url"].startswith("https://www.bcb.gov.br/estabilidadefinanceira/")


def test_kind_for_the_three_regimes():
    assert bcb_regimes.kind_for("Intervenção") == "intervencao"
    assert bcb_regimes.kind_for("Regime de Administração Especial Temporária") == "raet"
    assert bcb_regimes.kind_for("Algo novo") is None


def test_fetch_active_reads_list_then_details():
    calls = []

    def get(url):
        calls.append(url)
        if "regime_resolucao_ativo" in url:
            return {"conteudo": [{"identificador": 100562, "NomeIf": "WILL", "Situacao": "Ativo"}]}
        return {"conteudo": [WILL_DETAIL]}

    rows = bcb_regimes.fetch_active(get=get)
    assert [r["cnpj"] for r in rows] == ["23862762"]
    assert calls[1].endswith("identificador=100562")


def test_failed_detail_keeps_the_regime_active_not_closed():
    """2026-11-11 06:50 run: two detail fetches hit the ingest budget, the regimes dropped out of
    ``active`` and merge_state recorded two live liquidações as CLOSED until the next run."""
    prev = {"active": {"100562": bcb_regimes.normalize(WILL_DETAIL)}}

    def get(url):
        if "regime_resolucao_ativo" in url:
            return {"conteudo": [{"identificador": 100562, "NomeIf": "WILL", "Situacao": "Ativo"}]}
        raise RuntimeError("exceeded 180s budget")

    rows = bcb_regimes.fetch_active(get=get, prev_active=prev["active"])
    assert [r["id"] for r in rows] == ["100562"]
    doc = bcb_regimes.merge_state(prev, rows, as_of="2026-11-11", detail=lambda rid: None)
    assert doc["closed"] == {} and "100562" in doc["active"]
    # a brand-new regime whose first detail fails is simply absent until the next run
    assert bcb_regimes.fetch_active(get=get) == []


def test_merge_state_moves_a_vanished_regime_to_closed_with_its_reason():
    prev = {"active": {"100562": bcb_regimes.normalize(WILL_DETAIL)}}
    closed_detail = bcb_regimes.normalize({**WILL_DETAIL, "Situacao": "Encerrado (Convolação em falência.)",
                                           "DataEncerramento": "2026-11-03T03:00:00Z"})
    doc = bcb_regimes.merge_state(prev, [], as_of="2026-11-06", detail=lambda rid: closed_detail)
    assert doc["active"] == {} and doc["closed"]["100562"]["situacao"].startswith("Encerrado")
    assert bcb_regimes.is_falencia_closure(doc["closed"]["100562"]["situacao"])
    # back on the list → reopened
    again = bcb_regimes.merge_state(doc, [bcb_regimes.normalize(WILL_DETAIL)], as_of="2026-11-07")
    assert "100562" in again["active"] and again["closed"] == {}


def test_update_state_never_overwrites_with_a_collapsed_fetch():
    class S3:
        def __init__(self):
            self.put = None

        def get_object(self, Bucket, Key):
            body = json.dumps({"active": {str(i): {"id": str(i)} for i in range(20)}}).encode()
            return {"Body": type("B", (), {"read": lambda self: body})()}

        def put_object(self, **kw):
            self.put = kw

    s3 = S3()
    out = bcb_regimes.update_state("b", s3=s3, get=lambda url: {"conteudo": []}, as_of="2026-11-06")
    assert "skipped" in out and s3.put is None


# --- #362 regime events --------------------------------------------------------------------------

def test_active_regime_is_a_regulatory_event_dated_at_the_decree():
    evs = distress_watch.regime_events(_doc(WILL_DETAIL), {"23862762": "will_bank"}, today=TODAY)
    assert len(evs) == 1
    ev = evs[0]
    assert ev["entity"] == "will_bank" and ev["kind"] == "liquidacao_extrajudicial"
    assert ev["date"] == "2026-01-21" and ev["observed"] == "2026-11-06"
    assert ev["source_kind"] == "regulatory" and ev["regime_status"] == "ativo"
    assert "Ato do Presidente Nº 1376" in ev["title"]


def test_untracked_regime_has_no_entity():
    ev = distress_watch.regime_events(_doc(WILL_DETAIL), {}, today=TODAY)[0]
    assert ev["entity"] is None and distress.untracked_key(ev) == "cnpj:23862762#liquidacao_extrajudicial"


def test_closure_into_falencia_is_a_falencia_event():
    closed = {**WILL_DETAIL, "Situacao": "Encerrado (Convolação em falência.)",
              "DataEncerramento": "2026-11-03T03:00:00Z"}
    evs = distress_watch.regime_events(_doc(closed=[closed]), {"23862762": "will_bank"}, today=TODAY)
    kinds = {e["kind"]: e for e in evs}
    assert set(kinds) == {"liquidacao_extrajudicial", "falencia"}
    assert kinds["falencia"]["date"] == "2026-11-03"
    assert all("observed" not in e for e in evs)


def test_observed_keeps_an_old_decree_alive_past_the_ttl():
    ev = distress_watch.regime_events(_doc(MASTER_DETAIL), {"33923798": "banco_master"},
                                      today=dt.date(2028, 6, 1))[0]
    idx = distress.merge_distress({}, [ev], today=dt.date(2028, 6, 1))
    rec = idx["records"]["banco_master#liquidacao_extrajudicial"]
    assert rec["first_seen"] == "2025-11-18" and rec["last_seen"] == "2028-06-01"
    assert rec["confidence"] == "regulatory" and rec["act"].startswith("Ato do Presidente")
    # re-seen next run: no inflated mention count
    idx2 = distress.merge_distress(idx, [ev], today=dt.date(2028, 6, 2))
    assert idx2["records"]["banco_master#liquidacao_extrajudicial"]["mentions"] == 1


# --- #363 CNPJ backfill --------------------------------------------------------------------------

def test_cnpj_backfill_by_exact_legal_name_and_by_own_email_domain():
    got = distress_watch.regime_cnpj_matches(_doc(WILL_DETAIL, MASTER_DETAIL), ENTITIES)
    assert sorted(got) == [("banco_master", "33923798", "legal_name"), ("will_bank", "23862762", "domain")]


def test_cnpj_backfill_skips_entities_that_have_a_cnpj_and_ambiguous_names():
    ents = ENTITIES + [{"entity_id": "master_dup", "aliases": ["BANCO MASTER"], "cnpj_roots": []}]
    got = distress_watch.regime_cnpj_matches(_doc(MASTER_DETAIL), ents)
    assert got == []  # two entities claim the name → no binding
    nu = {**WILL_DETAIL, "Emails": "x@nubank.com.br"}
    assert distress_watch.regime_cnpj_matches(_doc(nu), ENTITIES[2:]) == []  # nubank already has a CNPJ


def test_a_brand_word_is_not_a_legal_name_match():
    ents = [{"entity_id": "master_card", "aliases": ["MASTER"], "cnpj_roots": []}]
    assert distress_watch.regime_cnpj_matches(_doc(MASTER_DETAIL), ents) == []


# --- #366 controllers ----------------------------------------------------------------------------

def test_controller_regime_propagates_a_group_row_to_the_controlled_entity():
    evs = distress_watch.regime_events(_doc(MASTER_DETAIL), {"33923798": "banco_master"}, today=TODAY)
    g = distress_watch.controlled_group_events(evs, ENTITIES)
    assert len(g) == 1
    assert g[0]["entity"] == "will_bank" and g[0]["role"] == "group" and g[0]["group_of"] == "banco_master"
    assert distress.record_key(g[0]) == "will_bank#group:liquidacao_extrajudicial"


def test_reported_news_distress_does_not_propagate_to_controlled_entities():
    ev = {"entity": "banco_master", "kind": "recuperacao_judicial", "role": "self", "source_kind": "news",
          "date": "2026-11-01", "title": "t", "label": "x"}
    assert distress_watch.controlled_group_events([ev], ENTITIES) == []


# --- #361 exits are not distress; run() end to end --------------------------------------------------

def _run(regimes, exits=()):
    return distress_watch.run(
        [], [], {}, entities=[{**e, "cnpj_roots": (["23862762"] if e["entity_id"] == "will_bank"
                                                   else ["33923798"] if e["entity_id"] == "banco_master"
                                                   else e["cnpj_roots"])} for e in ENTITIES],
        bcb_rows=[], reviews_fn=lambda: [], promote_fn=None, correspondent_lookup=None, qsa={},
        exits=list(exits), today=TODAY, regimes=regimes)


def test_run_records_regimes_and_ignores_register_exits():
    exits = [{"cnpj": "03461243", "name": "COOPERATIVA DE CRÉDITO DO NORTE CATARINENSE - ACREDICOOP",
              "date": "2026-11-06"}]
    out = _run(_doc(WILL_DETAIL, MASTER_DETAIL), exits)
    keys = sorted(distress.record_key(e) for e in out["tracked"])
    assert keys == ["banco_master#liquidacao_extrajudicial", "will_bank#group:liquidacao_extrajudicial",
                    "will_bank#liquidacao_extrajudicial"]
    assert out["untracked"] == []  # the merger exit is not a distress record
    assert out["summary"]["regimes_tracked"] == 2 and out["summary"]["controlled_group"] == 1


def test_regime_correspondent_links_need_the_regime_cnpj():
    ev = distress_watch.regime_events(_doc({**WILL_DETAIL, "Nome": "FRENTE SOCIEDADE CORRETORA DE CÂMBIO S.A.",
                                            "Cnpj": "71677850000100"}), {}, today=TODAY)[0]
    rows = [{"NomeCorrespondente": "FRENTE LOJA LTDA", "CnpjCorrespondente": "11111111000100",
             "CnpjContratante": "18236120000158", "NomeContratante": "NU"},
            {"NomeCorrespondente": "FRENTE SOCIEDADE CORRETORA DE CAMBIO S.A.", "CnpjCorrespondente": "71677850000100",
             "CnpjContratante": "18236120000158", "NomeContratante": "NU"}]
    distress_watch.attach_correspondents([ev], {"18236120": "nubank"}, lookup=lambda b: rows)
    assert [x["cnpj"] for x in ev.get("correspondent_of") or []] == ["71677850"]


# --- #365 status mentions ------------------------------------------------------------------------

def _resolver(item):
    t = item.get("title", "").lower()
    return [e for e, k in (("banco_master", "master"), ("will_bank", "will bank"), ("bb", "banco do brasil"))
            if k in t]


STATUS_TITLES = [
    "Liquidante do Banco Master diz que não há registros de patrocínio",
    "FGC inicia pagamento a 312 mil credores do Will Bank",
    "Will Bank quebrou, e agora?",
    "Após o colapso do Banco Master, o BC propõe mudanças",
]


def test_status_mentions_classify_as_status_not_as_events():
    for t in STATUS_TITLES:
        assert distress.classify_distress(t) is None and distress.status_mention(t), t
    for t in ("Itaú quebra recorde de lucro", "Credores do Banco do Brasil aprovam plano",
              "Banco Master e Will Financeira leiloam 445 lotes"):
        assert not distress.status_mention(t), t


def test_status_mentions_refresh_existing_trusted_records_only():
    items = [{"id": f"n{i}", "title": t, "url": f"https://g1.globo.com/{i}", "date": "2026-11-05"}
             for i, t in enumerate(STATUS_TITLES)]
    status: list = []
    events = distress.detect_distress_events(items, resolver=_resolver, today=TODAY, status=status)
    assert events == [] and {s["entity"] for s in status} == {"banco_master", "will_bank"}
    reg = distress_watch.regime_events(_doc(MASTER_DETAIL), {"33923798": "banco_master"}, today=TODAY)
    idx = distress.merge_distress({}, reg, today=TODAY)
    n = distress.refresh_status(idx, status)
    rec = idx["records"]["banco_master#liquidacao_extrajudicial"]
    assert n == 2 and rec["status_mentions"] == 2 and rec["mentions"] == 1
    assert "will_bank#liquidacao_extrajudicial" not in idx["records"]  # never creates
    assert distress.refresh_status(idx, status) == 0  # idempotent per evidence id


def test_status_mentions_do_not_refresh_a_single_outlet_report():
    idx = {"records": {"bb#falencia": {"entity": "bb", "kind": "falencia", "confidence": "reported"}}}
    assert distress.refresh_status(idx, [{"entity": "bb", "title": "x", "evidence_id": "1"}]) == 0


# --- #364 lifecycle gate -------------------------------------------------------------------------

def _feed():
    reg = distress_watch.regime_events(_doc(WILL_DETAIL), {"23862762": "will_bank"}, today=TODAY)
    idx = distress.merge_distress({}, reg, today=TODAY)
    return {
        "distress": distress.list_records(idx),
        "entity_attrs": {"will_bank": {"label": "Will Bank", "certifications": [
            {"label": "Autorização BCB", "source": "BCB", "basis": "industry"},
            {"label": "Autorização BCB", "source": "BCB", "basis": "register"},
            "BCB · Financeira · em funcionamento"]}},
        "swot": {"will_bank": {"label": "Will Bank", "counts": {"S": 1, "W": 1, "O": 1, "T": 0},
                               "dimensions": {"S": [{"text": "s", "status": "active"}],
                                              "W": [{"text": "w", "status": "active"}],
                                              "O": [{"text": "o", "status": "active"}], "T": []}}},
        "ansoff": {"will_bank": [{"text": "expandindo com a Mastercard"}], "nubank": [{"text": "ok"}]},
        "four_corners": {"will_bank": [{"text": "x"}]}, "tows": {"will_bank": [{"text": "SO"}]},
        "swot_proposals": [{"entity": "will_bank", "dimension": "O"}, {"entity": "will_bank", "dimension": "T"}],
        "feed": [{"id": "behavioral-will_bank-multi_front", "entity": "will_bank"},
                 {"id": "cand-ent-will_bank", "entity": "will_bank"}],
    }


def test_lifecycle_gate_withholds_growth_content_of_a_liquidated_entity():
    feed = _feed()
    life = lifecycle.apply_gate(feed)
    assert life["will_bank"]["status"] == "em_liquidacao" and life["will_bank"]["since"] == "2026-01-21"
    a = feed["entity_attrs"]["will_bank"]
    assert a["lifecycle"]["gated"] and [c["basis"] for c in a["certifications"]] == ["register"]
    sw = feed["swot"]["will_bank"]
    assert sw["dimensions"]["S"] == [] and sw["dimensions"]["O"] == [] and sw["dimensions"]["W"]
    assert sw["counts"]["O"] == 0 and sw["counts"]["W"] == 1
    assert "will_bank" not in feed["ansoff"] and "nubank" in feed["ansoff"]
    assert "will_bank" not in feed["four_corners"] and "will_bank" not in feed["tows"]
    assert feed["swot_proposals"] == [{"entity": "will_bank", "dimension": "T"}]
    assert [c["id"] for c in feed["feed"]] == ["cand-ent-will_bank"]
    assert feed["feed"][0]["lifecycle"] == "Em liquidação extrajudicial"


def test_lifecycle_gate_skips_raet_reported_and_closed_regimes():
    rows = [
        {"entity": "a", "kind": "raet", "role": "self", "confidence": "regulatory", "regime_status": "ativo"},
        {"entity": "b", "kind": "falencia", "role": "self", "confidence": "reported"},
        {"entity": "c", "kind": "liquidacao_extrajudicial", "role": "self", "confidence": "regulatory",
         "regime_status": "Encerrado (Aprovação de contas e baixa no registro público.)"},
        {"entity": "d", "kind": "liquidacao_extrajudicial", "role": "group", "confidence": "regulatory"},
    ]
    life = lifecycle.lifecycle_map(rows)
    assert set(life) == {"a"} and life["a"]["gated"] is False


def test_posture_panel_skips_gated_entities():
    feed = _feed()
    lifecycle.apply_gate(feed)
    feed["swot"]["nubank"] = {"label": "Nubank", "counts": {"S": 1}, "dimensions": {}}
    ents = {r["entity"] for r in executive._posture_rows(feed)}
    assert ents == {"nubank"}


# --- #362 history (BCB's own closed-regime list) ---------------------------------------------------

def test_history_fetches_details_only_for_tracked_names():
    calls = []
    closed_falencia = {**MASTER_DETAIL, "identificador": 9, "Nome": "BANCO EXEMPLO S.A.", "Cnpj": "12345678000199",
                       "Situacao": "Encerrado (Falência.)", "DataInicio": "2024-01-10T03:00:00Z",
                       "DataEncerramento": "2025-06-01T03:00:00Z"}

    def get(url):
        calls.append(url)
        if url.endswith("ParametroSituacao"):
            return {"conteudo": [{"Nome": "Ativo"}, {"Nome": "Encerrado (Falência.)"}]}
        if "regime_resolucao_ativo" in url:
            return {"conteudo": [{"identificador": 9, "NomeIf": "BANCO EXEMPLO S.A."},
                                 {"identificador": 8, "NomeIf": "OUTRA FINANCEIRA LTDA"}]}
        return {"conteudo": [closed_falencia]}

    doc = {"active": {}}
    n = bcb_regimes.update_history(doc, {}, as_of="2026-11-06", match_names={"banco exemplo"}, get=get)
    assert n == 1 and set(doc["history_detail"]) == {"9"} and len(doc["history"]["rows"]) == 2
    assert sum("Detalhamento" in c for c in calls) == 1
    # a week later: no list refetch, no detail refetch
    calls.clear()
    bcb_regimes.update_history(doc, doc, as_of="2026-11-08", match_names={"banco exemplo"}, get=get)
    assert calls == []
    # bound only by CNPJ, and a falência closure is a falência event
    evs = distress_watch.regime_events(doc, {"12345678": "exemplo"}, today=TODAY)
    assert {e["kind"] for e in evs} == {"liquidacao_extrajudicial", "falencia"}
    assert distress_watch.regime_events(doc, {}, today=TODAY) == []  # name alone never binds


def test_history_older_than_the_window_is_ignored():
    old = bcb_regimes.normalize({**MASTER_DETAIL, "Cnpj": "12345678000199", "DataInicio": "1996-01-01T03:00:00Z",
                                 "DataEncerramento": "2001-01-01T03:00:00Z", "Situacao": "Encerrado (Falência.)"})
    doc = {"history_detail": {"1": old}}
    assert distress_watch.regime_events(doc, {"12345678": "x"}, today=TODAY) == []


def test_a_domain_shared_by_a_group_identifies_no_one_but_the_legal_name_still_binds():
    shared = "liquidante@bancomaster.com.br"
    master = {**MASTER_DETAIL, "Emails": shared}
    multiplo = {**MASTER_DETAIL, "identificador": 100601, "Nome": "BANCO MASTER MÚLTIPLO S.A.",
                "Cnpj": "33884941000100", "Emails": shared}
    ents = [{"entity_id": "banco_master", "aliases": ["BANCO MASTER"], "cnpj_roots": []},
            {"entity_id": "bm_slug", "aliases": ["BANCOMASTER"], "cnpj_roots": []}]
    got = distress_watch.regime_cnpj_matches(_doc(master, multiplo), ents)
    assert got == [("banco_master", "33923798", "legal_name")]
