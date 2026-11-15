"""#324 board/v1 envelope + position contracts and the one-re-prompt rule."""
import json

import pytest

from src.board import contracts as c

SHA = "a" * 64
PACK = {"f-km1-itau-2026q2", "f-npl-bradesco-2026q2"}


def env(phase="diagnosis", prior=None):
    return c.make_envelope(run_id="r1", phase=phase, shared_context_id=SHA,
                           task="Rebaixamento de rating do banco X", prior_positions=prior)


def good(officer="risk", **over):
    pos = {"officer": officer, "diagnosis": "Capital acima do mínimo (2026T2).",
           "constraints": ["defasagem trimestral"],
           "options": [{"id": "opt-1", "description": "Reforçar liquidez",
                        "customer_impact": "low", "capital_liquidity_effect": "+LCR",
                        "red_lines": []}],
           "citations": [{"pack_fact_id": "f-km1-itau-2026q2", "source": "BCB KM1",
                          "as_of": "2026-06-30"}],
           "tool_call_summary": []}
    pos.update(over)
    return pos


def test_board_officers_are_the_five_adr032_boards():
    assert c.BOARD_OFFICERS == ("cso", "risk", "cco", "cpo", "cro")
    assert "cfo" not in c.BOARD_OFFICERS


def test_envelope_shape_and_guards():
    e = env()
    assert e["output_schema"] == "board/v1/position" and e["prior_positions"] == []
    with pytest.raises(ValueError):
        c.make_envelope(run_id="r", phase="vote", shared_context_id=SHA, task="t")
    with pytest.raises(ValueError):
        c.make_envelope(run_id="r", phase="diagnosis", shared_context_id="abc", task="t")
    with pytest.raises(ValueError):
        c.make_envelope(run_id="r", phase="diagnosis", shared_context_id=SHA, task="  ")


def test_valid_position_has_no_errors():
    assert c.validate_position(good(), officer="risk", pack_fact_ids=PACK) == []


@pytest.mark.parametrize("over,needle", [
    ({"officer": "cso"}, "officer must be"),
    ({"diagnosis": ""}, "diagnosis"),
    ({"citations": []}, "citations"),
    ({"citations": [{"pack_fact_id": "f-invented"}]}, "not in the pack"),
    ({"options": [{"id": "o", "description": "d", "customer_impact": "severe"}]}, "customer_impact"),
    ({"options": [{"id": "o", "description": "d", "customer_impact": "low"}] * 2}, "repeated"),
    ({"options": [{"id": f"o{i}", "description": "d", "customer_impact": "low"} for i in range(4)]},
     "at most"),
    ({"constraints": "x"}, "constraints"),
])
def test_invalid_positions(over, needle):
    errs = c.validate_position(good(**over), officer="risk", pack_fact_ids=PACK)
    assert any(needle in e for e in errs), errs


def test_parse_position_bare_fenced_and_garbage():
    obj = good()
    assert c.parse_position(json.dumps(obj)) == obj
    assert c.parse_position("Segue:\n```json\n" + json.dumps(obj) + "\n```\nfim") == obj
    assert c.parse_position("sem json") is None
    assert c.parse_position(None) is None


def test_prompts_carry_persona_and_contract_but_never_the_pack():
    sys_p, user_p = c.system_prompt("risk"), c.user_prompt("risk", env(prior=[{"officer": "cso"}]))
    assert "Oficial de risco" in sys_p and "outside-in" in sys_p
    assert '"officer": "risk"' in user_p and "Posições anteriores" in user_p
    assert "pack_fact_id" in user_p


def test_ask_officer_first_try_ok():
    calls = []
    out = c.ask_officer("risk", env(), call=lambda s, p: calls.append(p) or json.dumps(good()),
                        pack_fact_ids=PACK)
    assert out["status"] == "ok" and out["attempts"] == 1 and len(calls) == 1
    assert out["position"]["officer"] == "risk" and out["phase"] == "diagnosis"


def test_ask_officer_reprompts_once_with_the_errors():
    replies = iter(["nada", json.dumps(good())])
    prompts = []
    out = c.ask_officer("risk", env(), call=lambda s, p: prompts.append(p) or next(replies),
                        pack_fact_ids=PACK)
    assert out["status"] == "ok" and out["attempts"] == 2
    assert "rejeitada" in prompts[1] and "no JSON" in prompts[1]


def test_ask_officer_records_invalid_after_second_failure():
    bad = json.dumps(good(citations=[{"pack_fact_id": "f-invented"}]))
    out = c.ask_officer("risk", env(), call=lambda s, p: bad, pack_fact_ids=PACK)
    assert out["status"] == "invalid" and out["attempts"] == 2 and "position" not in out
    assert any("not in the pack" in e for e in out["errors"])


def test_ask_officer_no_answer_is_not_retried():
    n = []
    out = c.ask_officer("cro", env(), call=lambda s, p: n.append(1) and None, pack_fact_ids=PACK)
    assert out["status"] == "no_answer" and len(n) == 1


def test_ask_officer_rejects_unknown_officer():
    with pytest.raises(ValueError):
        c.ask_officer("cfo", env(), call=lambda s, p: "{}", pack_fact_ids=PACK)


def test_length_limits_are_stated_and_enforced():
    user = c.user_prompt("risk", env())
    assert f"até {c.DIAGNOSIS_TARGET} caracteres" in user
    long_diag = good(diagnosis="x" * (c.DIAGNOSIS_MAX + 1))
    assert any("diagnosis longer" in e for e in c.validate_position(long_diag, officer="risk", pack_fact_ids=PACK))
    opt = dict(good()["options"][0], description="y" * (c.DESCRIPTION_MAX + 1),
               red_lines=["a"] * (c.MAX_RED_LINES + 1))
    errs = c.validate_position(good(options=[opt], constraints=["c"] * 6), officer="risk", pack_fact_ids=PACK)
    assert any("description longer" in e for e in errs)
    assert any("red_lines" in e for e in errs) and any("constraints" in e for e in errs)
