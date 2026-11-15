"""#327 board pack artifact + the human gate (decide_board_brief → record_decision)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.board import artifact as A
from src.board import store
from src.dashboard import act_api
from test_act_api import _me
from test_board_act_mcp import board  # noqa: F401  (fixture)

RUN = {
    "run_id": "r1", "status": "complete", "target": "banco-aurora", "task": "Como reagir ao rebaixamento?",
    "feed_as_of": "2026-11-15", "peers": ["banco-boreal"], "pack_sha": "a" * 64,
    "synthesis": {
        "summary": "Aurora já era o mais frágil.",
        "options": [
            {"id": "opt-1", "description": "Funding | liquidez primeiro", "customer_impact": "low",
             "capital_liquidity_effect": "Protege LCR.", "citations": ["f-km1-aurora"],
             "supported_by": ["cso", "risk"], "contested_by": ["cco"]},
            {"id": "opt-2", "description": "Reprecificar crédito", "customer_impact": "high",
             "capital_liquidity_effect": "", "citations": ["f-npl-aurora"],
             "supported_by": ["cpo"], "contested_by": []}],
        "residual_regulatory_risk": "PAS em curso.",
        "monitoring_triggers": [{"text": "Próximo KM1", "citations": ["f-km1-aurora"]}]},
    "dissents": [{"officer": "cco", "challenged": ["cpo", "risk"], "text": "Discordo — conduta.",
                  "red_lines": ["sem venda casada"]}],
    "gaps": [{"officer": "cpo", "phase": "quantify", "gap": "sem resposta no prazo (180 s)"}],
    "pack_gaps": ["Sem dados internos."], "dropped": ["gatilho sem citação descartado: boato"],
    "cited_facts": [{"id": "f-km1-aurora", "source": "Pilar 3", "as_of": "2026-3", "text": "Basileia 12.1%"}],
    "phase_status": {"diagnosis": {"cso": "ok", "cpo": "invalid"}, "synthesize": {"secretary": "ok"}},
    "usage": {"calls": 12, "tokens_in": 1000, "tokens_out": 200, "token_cap": 400000},
}


def test_markdown_has_every_section_and_hides_nothing():
    md = A.render_markdown(RUN)
    for needle in ("# Briefing de conselho — banco-aurora", "outside-in", "## Situação", "## Opções",
                   "| opt-1 | Funding / liquidez primeiro | baixo | — | Estratégia, Risco | Compliance |",
                   "| opt-2 | Reprecificar crédito | alto | — | Produto | — |",
                   "### Compliance contesta Produto, Risco", "Discordo — conduta.", "- sem venda casada",
                   "## Risco regulatório residual", "- Próximo KM1 [f-km1-aurora]",
                   "sem resposta no prazo (180 s)", "Sem dados internos.", "descartado: gatilho sem citação",
                   "| f-km1-aurora | Pilar 3 | 2026-3 | Basileia 12.1% |", f"`{'a' * 64}`",
                   "Diagnóstico: Estratégia ok, Produto invalid", "Síntese: secretaria ok", "12 chamadas"):
        assert needle in md, needle


def test_markdown_of_a_partial_run_without_synthesis():
    md = A.render_markdown({"run_id": "r2", "status": "partial", "target": "x", "task": "t",
                            "gaps": [{"gap": "limite de custo atingido"}]})
    assert "parcial" in md and "limite de custo atingido" in md and "## Opções" not in md


def test_cited_facts_and_phase_status_helpers():
    pack = {"facts": [{"id": "a", "source": "s", "as_of": "d", "text": "t", "kind": "k"},
                      {"id": "b", "source": "s", "as_of": "d", "text": "u"}]}
    syn = {"options": [{"citations": ["b"]}], "monitoring_triggers": [{"citations": ["a"]}]}
    assert [f["id"] for f in A.cited_facts(syn, pack)] == ["a", "b"]
    assert "kind" not in A.cited_facts(syn, pack)[0] and A.cited_facts(None, pack) == []
    assert A.phase_status({"phases": {"diagnosis": {"risk": {"status": "ok"}}}}) == {"diagnosis": {"risk": "ok"}}


# ---- the gate over /api/act --------------------------------------------------------------------

def _seed(table, status="complete"):
    store.create_run(table, tenant="acme", run_id="r1", task=RUN["task"], target=RUN["target"])
    store.set_status(table, tenant="acme", run_id="r1", status=status,
                     **{k: RUN[k] for k in ("synthesis", "dissents", "gaps", "pack_sha", "cited_facts",
                                            "phase_status", "usage", "feed_as_of")})


def _decide(**args):
    r = act_api.lambda_handler(_me({"intent": "decide_board_brief", "args": {"run_id": "r1", **args}}), None)
    return r["statusCode"], json.loads(r["body"])


@pytest.fixture
def decisions(monkeypatch):
    from src.synth import decision_log
    seen = []

    def rec(**k):
        seen.append(k)
        return {"decision_id": f"d{len(seen)}", "verdict": k["verdict"]}
    monkeypatch.setattr(decision_log, "record_decision", rec)
    return seen


def test_accept_records_one_decision_linked_to_the_run(board, decisions):  # noqa: F811
    _, table = board
    _seed(table)
    code, b = _decide(choice="aceitar", option_id="opt-1", rationale="Prioridade é liquidez.")
    assert code == 200 and b["decision_id"] == "d1" and b["next"] == ["set_board_adoption", "set_outcome"]
    d = decisions[0]
    assert d["verdict"] == "aprovado" and d["recommendation"] == "Funding | liquidez primeiro"
    assert d["context_id"] == "board:r1" and d["action_ref"] == "opt-1" and d["evidence_id"] == "a" * 64
    assert d["tenant"] == "acme" and d["officer"] == "board"
    code2, b2 = _decide(choice="rejeitar")
    assert code2 == 409 and len(decisions) == 1                  # one decision per run


def test_modify_needs_text_and_keeps_its_origin(board, decisions):  # noqa: F811
    _, table = board
    _seed(table)
    assert _decide(choice="modificar", option_id="opt-2", text="curta")[0] == 400
    code, _ = _decide(choice="modificar", option_id="opt-2",
                      text="Reprecificar só cartão rotativo, com régua de renegociação.")
    assert code == 200
    d = decisions[0]
    assert d["recommendation"].startswith("Reprecificar só cartão") and "opção opt-2" in d["rationale"]


def test_reject_and_bad_requests(board, decisions):  # noqa: F811
    _, table = board
    _seed(table)
    assert _decide(choice="talvez")[0] == 400
    assert _decide(choice="aceitar", option_id="opt-9")[0] == 400
    assert _decide(choice="aceitar", option_id="opt-1", run_id="nope")[0] == 404
    code, b = _decide(choice="rejeitar", rationale="Fora do apetite.")
    assert code == 200 and decisions[0]["verdict"] == "rejeitado" and b["option_id"] is None


def test_a_run_without_options_cannot_be_decided(board, decisions):  # noqa: F811
    _, table = board
    store.create_run(table, tenant="acme", run_id="r1", task="t", target="x")
    store.set_status(table, tenant="acme", run_id="r1", status="failed", reason="pack")
    assert _decide(choice="rejeitar")[0] == 409 and decisions == []


def test_status_returns_the_board_pack_and_the_linked_decision(board, decisions):  # noqa: F811
    _, table = board
    _seed(table)
    _decide(choice="aceitar", option_id="opt-1")
    r = act_api.lambda_handler(_me({"intent": "board_run_status", "args": {"run_id": "r1"}}), None)
    b = json.loads(r["body"])
    assert b["board_pack"].startswith("# Briefing de conselho — banco-aurora")
    assert b["decision"]["decision_id"] == "d1" and b["decision"]["choice"] == "aceitar"
    listed = json.loads(act_api.lambda_handler(_me({"intent": "board_run_status", "args": {}}), None)["body"])
    assert [x["run_id"] for x in listed["runs"]] == ["r1"]       # the decision item is not a run


def test_gate_is_self_service_and_a_decision_intent():
    assert "decide_board_brief" in act_api._SELF_SERVICE and "decide_board_brief" in act_api._DECISION_INTENTS
