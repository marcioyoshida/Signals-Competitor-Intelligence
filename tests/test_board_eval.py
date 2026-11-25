"""Board eval scoring harness and release gate (#340)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.board import evaluate as ev
from src.board import store

PACK = {"sha256": "a" * 64, "scenario": "t-1", "facts": [
    {"id": "f-cap", "text": "Folga de Basileia de -1,29 p.p. em 2012-06; ROE de -34,3%.", "source": "IF.data", "as_of": "2012-06"},
    {"id": "f-liq", "text": "Captações caíram 13,47% no trimestre.", "source": "IF.data", "as_of": "2012-06"},
    {"id": "f-px", "text": "Ação caiu 26,1% em 5 dias contra o setor.", "source": "B3", "as_of": "2012-10-17"},
]}

CHAL = "O Risco contesta: sem aporte, a folga negativa leva o Banco Central a agir."


def _result():
    pos = lambda off, cites, opts=None: {"status": "ok", "position": {  # noqa: E731
        "officer": off, "diagnosis": CHAL if off == "risk" else f"diag {off}",
        "options": opts or [{"id": "opt-1", "red_lines": ["não distribuir dividendos"] if off == "risk" else []}],
        "citations": [{"pack_fact_id": c} for c in cites]}}
    return {
        "run_id": "dry-t-1-20261125T0000Z", "task": "Pergunta?", "status": "complete", "elapsed_s": 300.0,
        "shared_context_id": "a" * 64, "gaps": [], "dropped": [],
        "phases": {"diagnosis": {o: pos(o, ["f-cap"]) for o in ("cso", "risk", "cco", "cpo", "cro")},
                   "challenge": {"risk": pos("risk", ["f-cap"])},
                   "quantify": {"risk": pos("risk", ["f-liq"])},
                   "synthesize": {"secretary": {"status": "ok"}}},
        "dissents": [{"officer": "risk", "challenged": ["cso"], "text": CHAL,
                      "red_lines": ["não distribuir dividendos"]}],
        "synthesis": {
            "summary": "Folga de Basileia negativa (-1,3 p.p.) e prejuízo: risco de intervenção do Banco Central.",
            "residual_regulatory_risk": "Resolução do CMN sobre capital mínimo; divulgação de fato relevante.",
            "options": [
                {"id": "opt-1", "description": "Aporte de capital do controlador em 30 dias",
                 "customer_impact": "low", "capital_liquidity_effect": "repõe a folga de capital",
                 "citations": ["f-cap"], "derived_from": [{"officer": "risk", "option": "opt-1"}],
                 "supported_by": ["risk"], "contested_by": ["risk"]},
                {"id": "opt-2", "description": "Linhas de liquidez e captação garantida pelo FGC; clientes informados",
                 "customer_impact": "medium", "capital_liquidity_effect": "alonga a captação",
                 "citations": ["f-liq"], "derived_from": [{"officer": "cso", "option": "opt-1"}],
                 "supported_by": ["cso"], "contested_by": []}],
            "monitoring_triggers": [{"text": "Inadimplência e provisões acima de 13,5%", "citations": ["f-liq"]}]}}


def _run(result=None, **kw):
    return {"model": "sonnet", "provider": "test", "scenario": "t-1", "pack_sha256": "a" * 64,
            "usage": {"calls": 12, "tokens_in": 200000, "tokens_out": 30000, "cache_read": 5, "token_cap": 400000},
            "calls": [{"officer": "risk", "phase": "diagnosis", "s": 20.0, "input_tokens": 100, "output_tokens": 10}],
            "result": result or _result(), **kw}


def test_figures_and_grounding():
    figs = ev.figures("Folga de -1,3 p.p., ROE −34%, 1.234,5 bi, 90 dias, em 2015, 3 opções, 13,47%")
    assert [w for w, _, _ in figs] == ["-1,3 p.p.", "34%", "1.234,5 bi", "13,47%"]   # bare ints skipped
    vals = ev._pack_values([PACK["facts"][0]["text"], PACK["facts"][1]["text"]])
    assert ev.grounded(-1.3, 1, vals)          # -1,29 written as -1,3
    assert ev.grounded(13.5, 1, vals)          # 13,47 rounded
    assert not ev.grounded(24.0, 0, vals)      # derived, not in the pack


def test_clean_run_passes():
    s = ev.score(_run(), PACK)
    assert s["gate"]["passed"], s["gate"]["fails"]
    assert s["citations"]["citation_validity"] == 1.0 and s["citations"]["citations"] == 10
    assert s["dissent"]["dissent_preservation"] == 1.0 and s["dissent"]["contested_options"] == 1
    assert s["regulatory"]["n"] >= 5
    assert s["cost"]["tokens"] == 230000 and s["latency"]["slowest_call_s"] == 20.0


def test_invalid_citation_fails():
    r = _result()
    r["phases"]["quantify"]["risk"]["position"]["citations"].append({"pack_fact_id": "f-ghost"})
    s = ev.score(_run(r), PACK)
    assert "quantify/risk:f-ghost" in s["citations"]["invalid"] and not s["gate"]["passed"]


def test_paraphrased_dissent_fails():
    r = _result()
    r["dissents"][0]["text"] = "O Risco discorda, em resumo."
    s = ev.score(_run(r), PACK)
    assert s["dissent"]["dissent_preservation"] == 0.0
    assert any("dissent" in f for f in s["gate"]["fails"])


def test_missing_dissent_fails():
    r = _result()
    r["dissents"] = []
    assert ev.score(_run(r), PACK)["dissent"]["problems"] == ["risk: challenge reply has no dissent"]


def test_untraced_or_duplicate_options_fail():
    r = _result()
    r["synthesis"]["options"][1]["derived_from"] = []
    r["synthesis"]["options"][1]["description"] = r["synthesis"]["options"][0]["description"]
    fails = ev.score(_run(r), PACK)["gate"]["fails"]
    assert any("traced" in f for f in fails) and any("overlap" in f for f in fails)


def test_ungrounded_figures_fail_below_bar():
    r = _result()
    r["synthesis"]["summary"] += " Corte de 24%, 31%, 47% e 52% nas taxas."
    s = ev.score(_run(r), PACK)
    assert s["citations"]["ungrounded_figures"] == ["24%", "31%", "47%", "52%"]
    assert any("numbers grounded" in f for f in s["gate"]["fails"])


def test_cost_latency_status_and_pack_mismatch():
    r = _result()
    r["status"], r["elapsed_s"] = "partial", 1200.0
    run = _run(r, pack_sha256="b" * 64)
    run["usage"]["tokens_in"] = 500000
    run["calls"].append({"officer": "cro", "phase": "challenge", "error": "timeout"})
    fails = " | ".join(ev.score(run, PACK)["gate"]["fails"])
    for want in ("status partial", "pack sha differs", "tokens", "elapsed 1200.0", "1 call errors"):
        assert want in fails


def test_episode_answer_key_and_signal():
    ep = {"schema": "board/v1/episode",
          "rubric": {"items": [{"id": "capital_shortfall", "any_of": ["folga negativa", "basileia"]},
                               {"id": "supervisory_action", "any_of": ["intervenção"]},
                               {"id": "asset_sales", "any_of": ["venda de ativos"]}]},
          "answer_key": {"decision_categories": {"capital_raise": 1, "accounts": 2, "governance": 1},
                         "pre_event_signal": {"visible": True, "basis": ["prudential"]}}}
    s = ev.score(_run(), PACK, episode=ep)
    e = s["episode"]
    assert e["rubric"]["covered"] == ["capital_shortfall", "supervisory_action"]
    assert e["answer_key"]["board_categories"] == ["capital_raise", "governance"]   # accounts is routine
    assert e["answer_key"]["matched"] == ["capital_raise"] and e["answer_key"]["alignment"] == 0.5
    assert e["signal"]["caught"]
    assert s["gate"]["passed"] and s["gate"]["warnings"] == []


def test_soft_checks_warn_but_do_not_fail():
    ep = {"rubric": {"items": [{"id": "x", "any_of": ["eurobond"]}, {"id": "y", "any_of": ["teto soberano"]}]},
          "answer_key": {"decision_categories": {"capital_return": 1},
                         "pre_event_signal": {"visible": True, "basis": ["market"]}}}
    s = ev.score(_run(), PACK, episode=ep)
    assert s["gate"]["passed"] and len(s["gate"]["warnings"]) == 3


def test_release_gate_uses_latest_run_per_scenario():
    ok = ev.score(_run(), PACK)
    old = copy.deepcopy(ok)
    old["run_id"], old["gate"] = "dry-t-1-20261101T0000Z", {"passed": False, "fails": ["x"], "warnings": []}
    assert ev.release_gate([old, ok], ["t-1"])["passed"]
    rg = ev.release_gate([old], ["t-1", "t-2"])
    assert not rg["passed"] and rg["missing"] == ["t-2"] and rg["failed"] == ["t-1"]


def test_fold_and_keyword_hits():
    assert ev.hits(ev.fold("Intervenção do BANCO CENTRAL"), ["intervencao", "banco central", "raet"]) == [
        "intervencao", "banco central"]
    assert ev.hits(ev.fold("capitalização"), ["capital"]) == ["capital"]       # word start
    assert ev.hits(ev.fold("recapitalização"), ["capital"]) == []


def test_gate_config_is_fixed_and_complete():
    g = ev.load_gate()
    assert g["version"] >= 1 and g["fixed_on"]
    if g["version"] > 1:
        assert str(g["version"]) in g["changes"]                         # every change is explained
    assert {i["id"] for i in g["regulatory_checklist"]} == {
        "capital", "liquidity", "credit", "conduct", "disclosure", "supervisor"}
    from src.board.episodes import CATEGORIES
    cats = {c for c, _ in CATEGORIES} - ev.ROUTINE
    assert cats <= set(g["decision_keywords"])                           # every board category scorable


def test_required_set_is_every_scenario_and_usable_episode():
    req = ev.required_set()
    assert {"downgrade-v1", "competitor-v2"} <= set(req)
    idx = json.loads((ev.EVAL_DIR / "episodes" / "index.json").read_text(encoding="utf-8"))
    assert {e["id"] for e in idx["episodes"] if e["usable"]} <= set(req)


def test_episode_packs_rebuild_deterministically():
    files = ev.scenario_files()
    sc = json.loads(files["fail-bva-2012"].read_text(encoding="utf-8"))
    a, task = ev.scenario_pack(sc)
    b, _ = ev.scenario_pack(json.loads(files["fail-bva-2012"].read_text(encoding="utf-8")))
    assert a["sha256"] == b["sha256"] and task == sc["task"] and a["focal"] == "ep-bva"


@pytest.mark.parametrize("name,passed", [("dry-downgrade-v1-20261115T0502Z", True),
                                         ("dry-competitor-v2-20261115T0738Z", True),
                                         ("dry-downgrade-v1-20261115T0445Z", False)])  # before derived_from
def test_recorded_dry_runs(name, passed):
    run = json.loads((ev.EVAL_DIR / "runs" / f"{name}.json").read_text(encoding="utf-8"))
    sc = json.loads(ev.scenario_files()[run["scenario"]].read_text(encoding="utf-8"))
    pack, _ = ev.scenario_pack(sc)
    s = ev.score(run, pack)
    assert s["pack_matches"] and s["citations"]["citation_validity"] == 1.0
    assert s["gate"]["passed"] is passed, s["gate"]["fails"]


class _Table:
    def __init__(self):
        self.items = {}

    def put_item(self, Item, ConditionExpression=None):
        k = (Item["tenant"], Item["sk"])
        if ConditionExpression and k in self.items:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[k] = Item


def test_put_eval_writes_once_under_eval_tenant():
    t = _Table()
    s = ev.score(_run(), PACK)
    assert store.put_eval(t, run_id=s["run_id"], scores=s)
    assert not store.put_eval(t, run_id=s["run_id"], scores=s)           # same gate version: once
    keys = sorted(k[1] for k in t.items)
    assert keys == [f"RUN#{s['run_id']}", f"RUN#{s['run_id']}#EVAL#v1"]
    assert all(k[0] == "_eval" for k in t.items)
    ev_item = t.items[("_eval", f"RUN#{s['run_id']}#EVAL#v1")]
    assert ev_item["passed"] is True and ev_item["expires_at"] > t.items[("_eval", f"RUN#{s['run_id']}")]["expires_at"] - 10
    assert f"RUN#{s['run_id']}".count("#") == 1                          # list_runs shows the run item only
