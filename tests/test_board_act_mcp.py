"""#328 board brief over /api/act (board_brief, board_run_status) and the tenant MCP tools."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.dashboard import act_api
from src.dashboard import mcp_server as m
from test_act_api import SECRET, _me, _no_journal
from test_board_store import FakeTable
from test_mcp_server import ctx

SM = "arn:aws:states:us-east-1:111111111111:stateMachine:onca-board-brief"


class FakeSfn:
    def __init__(self):
        self.started = []

    def start_execution(self, stateMachineArn, name, input):
        if any(n == name for _, n, _ in self.started):
            e = Exception("exists")
            e.response = {"Error": {"Code": "ExecutionAlreadyExists"}}
            raise e
        self.started.append((stateMachineArn, name, json.loads(input)))
        return {"executionArn": "x"}


@pytest.fixture
def board(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    monkeypatch.setenv("ONCA_BOARD_STATE_MACHINE", SM)
    monkeypatch.setenv("ONCA_BOARD_RUNS_TABLE", "runs")
    sfn, table = FakeSfn(), FakeTable()
    import boto3
    monkeypatch.setattr(boto3, "client", lambda svc, *a, **k: sfn if svc == "stepfunctions" else None)
    monkeypatch.setattr(boto3, "resource", lambda svc, *a, **k: type("R", (), {"Table": lambda self, n: table})())
    from src.dashboard import tenant_config
    cfgs = {"acme": {"tenant_id": "acme", "modules": ["banking"], "billing": {"state": "active"}},
            "trialco": {"tenant_id": "trialco", "modules": ["banking"], "billing": {"state": "trial"}}}
    monkeypatch.setattr(tenant_config, "get_tenant_config", lambda t, **k: cfgs.get(t))
    monkeypatch.setattr(tenant_config, "_table",
                        lambda t=None: type("T", (), {"get_item": lambda self, Key: {"Item": cfgs.get(Key["tenant_id"])}})())
    from src.synth import model_keys
    monkeypatch.setattr(model_keys, "configured", lambda: False)
    return sfn, table


def brief(tenant="acme", key="question-0001", extra=None, **args):
    body = {"intent": "board_brief", "args": {"target": "bradesco",
                                              "task": "Como responder a um rebaixamento de rating?",
                                              **args, **(extra or {})}}
    if key:
        body["idempotency_key"] = key
    return act_api.lambda_handler(_me(body, claims={"sub": "u1", "custom:tenant": tenant}), None)


def body(resp):
    return json.loads(resp["body"])


def test_board_intents_are_in_the_catalog_and_self_service():
    assert {"board_brief", "board_run_status"} <= set(act_api.catalog())
    assert {"board_brief", "board_run_status"} <= act_api._SELF_SERVICE


def test_paying_tenant_starts_one_execution_named_after_the_run(board):
    sfn, _ = board
    r = brief()
    assert r["statusCode"] == 202
    b = body(r)
    assert b["status"] == "queued" and b["gate"] == "paying" and b["next"] == "board_run_status"
    arn, name, inp = sfn.started[0]
    assert arn == SM and name == f"brief-{b['run_id']}"
    assert inp["tenant"] == "acme" and inp["idempotency_key"] == "question-0001" and inp["requested_by"] == "u1"
    again = brief()
    assert again["statusCode"] == 200 and body(again)["status"] == "exists"
    assert body(again)["run_id"] == b["run_id"] and len(sfn.started) == 1


def test_tenant_comes_from_the_token_not_the_args(board):
    sfn, _ = board
    brief(extra={"tenant_id": "other", "tenant": "other"})
    assert sfn.started[0][2]["tenant"] == "acme"


def test_trial_tenant_is_refused_before_anything_starts(board):
    sfn, _ = board
    r = brief(tenant="trialco")
    assert r["statusCode"] == 402 and body(r)["reason"] == "not_paying:trial"
    assert sfn.started == []


def test_key_target_and_task_are_required(board):
    assert brief(key=None)["statusCode"] == 400
    assert brief(task="curta")["statusCode"] == 400
    assert brief(target="")["statusCode"] == 400


def test_disabled_when_the_state_machine_is_not_configured(board, monkeypatch):
    monkeypatch.delenv("ONCA_BOARD_STATE_MACHINE")
    assert brief()["statusCode"] == 503


def test_run_status_reads_only_the_callers_tenant(board):
    _, table = board
    from src.board import store
    store.create_run(table, tenant="acme", run_id="r1", task="t", target="bradesco")
    store.set_status(table, tenant="acme", run_id="r1", status="complete", usage={"calls": 12})
    store.create_run(table, tenant="other", run_id="r2", task="t", target="itau")

    def status(**args):
        return act_api.lambda_handler(_me({"intent": "board_run_status", "args": args}), None)
    one = status(run_id="r1")
    assert one["statusCode"] == 200 and body(one)["run"]["status"] == "complete"
    assert body(one)["run"]["usage"] == {"calls": 12}
    assert status(run_id="r2")["statusCode"] == 404          # another tenant's run
    assert [r["run_id"] for r in body(status())["runs"]] == ["r1"]


def test_run_status_is_never_replayed_or_journaled():
    assert "board_run_status" in act_api._NO_REPLAY and "board_run_status" in act_api._NO_JOURNAL


# ---- MCP -------------------------------------------------------------------------------------

def tools(c, path="/mcp"):
    r = m.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, path=path, ctx=c)
    return [t["name"] for t in r["result"]["tools"]]


def test_board_tools_are_listed_only_to_a_tenant_caller():
    assert {"board_brief", "board_run_status"} <= set(tools(ctx()))
    no_tenant = ctx()
    no_tenant.principal = {"sub": "u9", "groups": []}
    assert not {"board_brief", "board_run_status"} & set(tools(no_tenant))
    assert "board_brief" in tools(ctx(elevated=True), "/mcp/ops")   # operators via the catalog


def test_board_tool_calls_route_through_act():
    acts = []
    c = ctx(acts=acts)

    def call(name, args):
        return m.handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": name, "arguments": args}}, path="/mcp", ctx=c)
    call("board_brief", {"target": "bradesco", "task": "Como reagir ao rebaixamento?",
                         "idempotency_key": "k-00000001"})
    call("board_run_status", {"run_id": "r1"})
    call("board_run_status", {})
    assert acts == [
        {"intent": "board_brief", "idempotency_key": "k-00000001",
         "args": {"target": "bradesco", "task": "Como reagir ao rebaixamento?"}},
        {"intent": "board_run_status", "args": {"run_id": "r1"}},
        {"intent": "board_run_status", "args": {}}]
    short = call("board_brief", {"target": "b", "task": "x" * 20, "idempotency_key": "k"})
    assert "idempotency_key" in json.dumps(short, ensure_ascii=False)
    assert len(acts) == 3


def test_board_tool_descriptions_state_the_outside_in_boundary():
    for t in m.board_tools():
        assert "never a capital, liquidity or pricing decision" in t["description"]
