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


def test_c2_the_entrant_gate_also_stops_the_supplementary_registries(monkeypatch):
    from src.ingest import susep_entidades
    seen = []
    monkeypatch.setattr(susep_entidades, "fetch_entities", lambda: seen.append(1) or [])
    _run(monkeypatch, {"ONCA_INGEST_SUSEP": "true"})
    assert seen == [1]
    seen.clear()
    out = _run(monkeypatch, {"ONCA_INGEST_SUSEP": "true", "ONCA_NEW_ENTRANTS": "false"})
    assert seen == [] and _calls(out, "autorizacoes.fetch") == [] and _calls(out, "receita.enrich") == []
