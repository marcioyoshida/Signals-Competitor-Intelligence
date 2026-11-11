"""ADR 019 completion items C1–C5 (#303), on top of the byte-identical golden digest
(tests/test_adr019_golden.py)."""
from __future__ import annotations

import json

from src.ingest import registry
from tests.test_adr019_golden import _run


def _calls(out, name):
    return [json.loads(c) for c in out["calls"] if json.loads(c)[0] == name]


# --- C1: moves + market in the loop ---------------------------------------------------------------
def test_c1_every_moves_and_snapshot_source_is_declared():
    for sid in ("pix_moves", "juros_moves", "inf_diario_moves", "fiagro_moves"):
        spec = registry.by_id(sid)
        assert spec.delta == "moves" and spec.tracked_key and spec.label
    for sid in ("pix_moves", "juros_moves", "inf_diario_moves"):
        assert registry.by_id(sid).key_field and registry.by_id(sid).value_field
    assert registry.by_id("market").delta == "none"


def test_c1_a_disabled_moves_source_fetches_nothing_and_keeps_its_section(monkeypatch):
    out = _run(monkeypatch, {"ONCA_FIAGRO_SIGNAL": "false"})
    assert _calls(out, "fiagro.fetch") == []
    fia = json.loads(out["digest"])["fiagro_moves"]
    assert fia == {"funds_tracked": 0, "pl_move_count": 0, "cotista_move_count": 0,
                   "new_registration_count": 0, "move_count": 0, "items": [], "context": []}


def test_c1_an_empty_watchlist_touches_no_state(monkeypatch):
    out = _run(monkeypatch, {"ONCA_COMPETITORS": "", "ONCA_INF_DIARIO_USE_COMPETITORS": "false"})
    assert _calls(out, "infd.fetch") == []
    assert not any("cvm_inf_diario" in s for s in out["state"])
    assert json.loads(out["digest"])["inf_diario_moves"]["as_of"] is None


# --- C2: side-effecting lens sources in the loop ---------------------------------------------------
def test_c2_sec_enriches_the_new_filings_and_the_context_inside_its_run(monkeypatch):
    out = _run(monkeypatch)
    enr = [c[1][0] for c in _calls(out, "sec.enrich")]
    assert [r["id"] for r in enr[0]] == ["sec-0", "sec-1"]             # the new filings first
    sec = json.loads(out["digest"])["sec_filings"]
    assert sec["items"] and all(i.get("text") for i in sec["items"])   # section built after it
    none = _run(monkeypatch, {"ONCA_SEC_TICKERS": ""})
    assert _calls(none, "sec.fetch") == [] and not any("sec_filings" in s for s in none["state"])


def test_c2_fatos_governance_first_and_alias_accrual_after_the_loop(monkeypatch):
    out = _run(monkeypatch)
    names = [c[1][1] for c in _calls(out, "er.accumulate")]
    assert ["Co0"] in names and ["Co1"] in names                       # new_fatos -> alias accrual
    none = _run(monkeypatch, {"ONCA_FATOS_WATCHLIST": "", "ONCA_FATOS_USE_COMPETITORS": "false",
                              "ONCA_FATOS_USE_REGISTRY": "false"})
    assert _calls(none, "fatos.fetch") == [] and not any("cvm_fatos" in s for s in none["state"])


def test_c2_the_fatos_post_hook_floats_governance_events(monkeypatch):
    from src.ingest import lambda_port as lp

    def swap(mp, rec):
        mp.setattr(lp.cvm_ipe, "fetch_material_facts", rec("fatos.fetch", [
            {"id": "f-a", "company": "A", "governance": False}, {"id": "f-b", "company": "B", "governance": True}]))
    fatos = json.loads(_run(monkeypatch, after=swap)["digest"])["fatos"]
    assert [i["id"] for i in fatos["items"]] == ["f-b", "f-a"] and fatos["governance_count"] == 1


def test_c2_new_entrants_side_effects_consume_the_loop_result(monkeypatch):
    out = _run(monkeypatch)
    assert [r["id"] for r in _calls(out, "autorizacoes.certs")[0][1][0]] == ["ent-0", "ent-1", "ent-2"]
    assert [r["id"] for r in _calls(out, "receita.enrich")[0][1][0]] == ["ent-0", "ent-1"]
    assert sorted(c[1][0]["id"] for c in _calls(out, "er.auto_create")) == ["ent-0", "ent-1"]
    sec = json.loads(out["digest"])["new_entrants"]
    assert sec["count"] == 3 and sec["new_count"] == 2


def test_c3_no_declared_knob_is_read_outside_spec_params():
    import re
    from pathlib import Path
    src = Path(registry.__file__).with_name("lambda_port.py").read_text(encoding="utf-8")
    # FIAGRO discovery (a registry job, not a source) shares the fund-size floor env var
    shared = {"ONCA_FIAGRO_MIN_PL"}
    for spec in registry.SOURCES:
        for p in spec.params:
            if p.env in shared:
                continue
            assert not re.search(rf'["\']{p.env}["\']', src), (spec.id, p.env)


def test_c3_defaults_are_the_established_values():
    P = registry.all_params({})
    assert P["regulatory"] == {"lookback_days": 7}
    assert P["ofertas"] == {"lookback_days": 30, "watchlist": [], "use_competitors": True}
    assert P["fatos"] == {"lookback_days": 45, "watchlist": [], "use_competitors": True,
                          "use_registry": True, "categories": None}
    assert P["dou"] == {"lookback_days": 30, "watchlist": [], "use_competitors": True,
                        "sector_topics": True, "max_topic_terms": 45}
    assert P["sec_filings"] == {"tickers": [], "lookback_days": 365}
    assert P["pix_moves"] == {"watchlist_ispb": [], "min_pct": 15.0}
    assert P["juros_moves"] == {"institutions": [], "modalities": [], "use_default_modalities": True,
                                "min_pct": 10.0}
    assert P["inf_diario_moves"] == {"watchlist": [], "use_competitors": True, "min_pct": 10.0, "top_n": None}
    assert P["fiagro_moves"] == {"min_pl": 50_000_000.0, "pl_min_pct": 15.0, "cotista_min_pct": 30.0,
                                 "new_reg_lookback_days": 60}
    assert P["contracts"] == {"lookback_days": 2, "max_pages": 60, "min_valor": 0.0}
    assert P["cade"] == {"lookback_days": 45} and P["cvm_normas"] == {"lookback_days": 30}
    assert P["market"] == {"max_resolve": 150}
    assert P["news"]["max_terms"] == 80 and P["news"]["digest_items"] == 150
    ne = P["new_entrants"]
    assert ne["receita_enrich"] and ne["autocreate"] and ne["autocreate_fintech_only"] and ne["review"]
    assert not (ne["ingest_susep"] or ne["ingest_spa"] or ne["ingest_previc"] or ne["autocreate_regulated"])


def test_c3_param_parsing_matches_the_old_env_reads():
    env = {"ONCA_COMPETITORS": " ALFA, ,BETA ", "ONCA_FATOS_CATEGORIES": " , ",
           "ONCA_INF_DIARIO_TOP_N": "  ", "ONCA_OFERTAS_USE_COMPETITORS": "YES",
           "ONCA_DOU_USE_COMPETITORS": "0", "ONCA_PIX_MOVE_THRESHOLD_PCT": "7.5"}
    P = registry.all_params(env)
    assert P["competitor"]["admins"] == ["ALFA", "BETA"]
    assert P["fatos"]["categories"] is None and P["inf_diario_moves"]["top_n"] is None
    assert P["ofertas"]["use_competitors"] is True and P["dou"]["use_competitors"] is False
    assert P["pix_moves"]["min_pct"] == 7.5
    assert registry.all_params({"ONCA_INF_DIARIO_TOP_N": "20"})["inf_diario_moves"]["top_n"] == 20


STORES = ("macro", "datajud", "bcb_reclamacoes", "reclame_aqui", "consumidor_gov", "ans_igr")


def test_c4_the_non_lens_stores_are_specs_without_a_lens_section():
    for sid in STORES:
        spec = registry.by_id(sid)
        assert spec.lens is None and spec.integration in ("store", "macro") and spec.env_flag
    assert registry.by_id("market").integration == "store"             # the IF.data store
    assert not set(STORES) & {sid for sid, _ in registry.section_lens_pairs()}
    from src.synth import candidates
    assert not set(STORES) & {sid for sid, _ in candidates._reg.section_lens_pairs()}


def test_c4_store_defaults_match_the_old_flags():
    from src.ingest import lambda_port as lp
    on = {sid: lp._source_enabled(registry.by_id(sid)) for sid in STORES}
    assert on == {"macro": True, "datajud": True, "bcb_reclamacoes": True,
                  "reclame_aqui": False, "consumidor_gov": False, "ans_igr": False}


def test_c4_stores_are_vertical_gated_by_the_same_path(monkeypatch):
    from src.ingest import lambda_port as lp
    monkeypatch.setenv("ONCA_VERTICAL", "pharma")
    assert not lp._source_enabled(registry.by_id("bcb_reclamacoes"))   # FS-only store
    assert lp._source_enabled(registry.by_id("macro"))                 # economy-wide
    monkeypatch.setenv("ONCA_VERTICAL", "health")
    monkeypatch.setenv("ONCA_ANS_IGR", "true")
    assert lp._source_enabled(registry.by_id("ans_igr"))               # health insurance


# --- C5: under a sector vertical no FS-only source fetches ----------------------------------------
EVERYTHING_ON = {"ONCA_SOUNDNESS": "true", "ONCA_INGEST_RECEITA_BULK": "true",
                 "ONCA_ENTITY_DISCOVERY": "true", "ONCA_DISCOVER_BCB": "true",
                 "ONCA_PROMOTE_REGULATED": "true", "ONCA_INGEST_SUSEP": "true", "ONCA_INGEST_SPA": "true",
                 "ONCA_INGEST_PREVIC": "true", "ONCA_INGEST_CVM_PARTICIPANTES": "true",
                 "ONCA_RECLAME_AQUI": "true", "ONCA_ANS_IGR": "true", "ONCA_CONSUMIDOR_GOV": "true",
                 "ONCA_PNCP_CONTRATOS": "true"}
# sector-agnostic ({all}) sources and the registry read their CNPJ index needs
AGNOSTIC = {"macro.selic", "macro.focus", "datajud.fetch", "cade.fetch", "cade.map", "ceis.index",
            "ceis.fetch", "ceis.map", "ceis.store", "er.list_entities", "consumidor.fetch",
            "pncp.fetch", "pncp.map", "pncp.store",
            "corpus"}                                       # the corpus writer (a sink, not a fetch)


def _stub_jobs(mp, rec):
    from src.ingest import (ans_igr, bcb_soundness, consumidor_gov, cvm_fii, cvm_participantes,
                            pncp_contratos, previc_efpc, receita_bulk, reclame_aqui, spa_apostas,
                            susep_entidades)
    from src.synth import entity_discovery as ed
    mp.setattr(bcb_soundness, "latest_base_date", rec("soundness.date", 202606))
    mp.setattr(bcb_soundness, "fetch_capital", rec("soundness.fetch", []))
    mp.setattr(bcb_soundness, "fetch_institution_names", rec("soundness.names", {}))
    mp.setattr(receita_bulk, "fetch_shard", rec("receita_bulk.fetch", []))
    mp.setattr(receita_bulk, "propose_candidates", rec("receita_bulk.propose",
                                                       {"already": 0, "proposed": [], "no_name": 0}))
    for fn in ("discover_fiagro", "discover_consorcio", "discover_bcb_institutions", "promote_roster"):
        mp.setattr(ed, fn, rec("discovery." + fn, {}))
    mp.setattr(cvm_fii, "fetch_fii", rec("fii.fetch", []))
    mp.setattr(susep_entidades, "fetch_entities", rec("susep.fetch", []))
    mp.setattr(spa_apostas, "fetch_authorized", rec("spa.fetch", []))
    mp.setattr(previc_efpc, "fetch_entities", rec("previc.fetch", []))
    mp.setattr(cvm_participantes, "fetch_participants", rec("participantes.fetch", []))
    mp.setattr(reclame_aqui, "companies_from_registry", rec("reclame.companies", []))
    mp.setattr(reclame_aqui, "fetch_reputation", rec("reclame.fetch", []))
    mp.setattr(ans_igr, "run", rec("ans_igr.run", None))
    mp.setattr(consumidor_gov, "fetch_indicators", rec("consumidor.fetch", []))
    mp.setattr(pncp_contratos, "fetch_contracts", rec("pncp.fetch", []))
    mp.setattr(pncp_contratos, "map_to_entities", rec("pncp.map", lambda rows, cnpj_index=None: rows))
    mp.setattr(pncp_contratos, "update_store", rec("pncp.store", None))


def _names(out):
    return {json.loads(c)[0] for c in out["calls"]}


def test_c5_a_sector_vertical_runs_no_fs_fetch(monkeypatch):
    fs = _names(_run(monkeypatch, EVERYTHING_ON, after=_stub_jobs))
    # positive control: under FS every one of these jobs is reached
    for n in ("soundness.date", "receita_bulk.fetch", "discovery.discover_fiagro", "susep.fetch",
              "reclame.companies", "ans_igr.run", "pix.fetch", "ifdata.load_index", "autorizacoes.fetch",
              "bcb_supervision.update"):
        assert n in fs, n
    sector = _names(_run(monkeypatch, {**EVERYTHING_ON, "ONCA_VERTICAL": "pharma"}, after=_stub_jobs))
    assert sector <= AGNOSTIC, sorted(sector - AGNOSTIC)
    assert {"macro.selic", "datajud.fetch", "cade.fetch", "consumidor.fetch"} <= sector


def test_c5_the_news_slice_fetches_nothing_outside_its_vertical(monkeypatch):
    from src.ingest import lambda_port as lp
    called = []
    monkeypatch.setattr(lp.trade_press, "fetch_news", lambda *a, **k: called.append(1) or [])
    monkeypatch.setattr(lp.trade_press, "fetch_sector_news", lambda *a, **k: called.append(1) or [])
    monkeypatch.setenv("ONCA_VERTICAL", "pharma")
    monkeypatch.setenv("ONCA_NEWS_WATCHLIST", "ALFA")
    assert lp._news_slice(None) == lp._empty_news_slice() and called == []


def test_c2_the_entrant_gate_also_stops_the_supplementary_registries(monkeypatch):
    from src.ingest import susep_entidades
    seen = []
    monkeypatch.setattr(susep_entidades, "fetch_entities", lambda: seen.append(1) or [])
    _run(monkeypatch, {"ONCA_INGEST_SUSEP": "true"})
    assert seen == [1]
    seen.clear()
    out = _run(monkeypatch, {"ONCA_INGEST_SUSEP": "true", "ONCA_NEW_ENTRANTS": "false"})
    assert seen == [] and _calls(out, "autorizacoes.fetch") == [] and _calls(out, "receita.enrich") == []
