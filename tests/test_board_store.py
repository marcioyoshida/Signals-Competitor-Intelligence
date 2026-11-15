"""#322 OncaBoardRuns store: idempotent run ids, append-only responses, terminal runs frozen."""
import datetime as dt
import re

import pytest

from src.board import store as S


class CCF(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeTable:
    """Just enough DynamoDB: put with attribute_not_exists(sk), update with the terminal guard."""

    def __init__(self):
        self.items = {}

    def put_item(self, Item, ConditionExpression=None):
        k = (Item["tenant"], Item["sk"])
        if ConditionExpression == "attribute_not_exists(sk)" and k in self.items:
            raise CCF()
        self.items[k] = dict(Item)

    def get_item(self, Key):
        it = self.items.get((Key["tenant"], Key["sk"]))
        return {"Item": dict(it)} if it else {}

    def update_item(self, Key, UpdateExpression, ConditionExpression, ExpressionAttributeNames,
                    ExpressionAttributeValues):
        k = (Key["tenant"], Key["sk"])
        it = self.items.get(k)
        assert ConditionExpression.startswith("attribute_exists(sk) AND NOT (#s IN (")
        terminal = {v for n, v in ExpressionAttributeValues.items() if n.startswith(":t")}
        if it is None or it.get("status") in terminal:
            raise CCF()
        for name, val in re.findall(r"(#\w+) = (:\w+)", UpdateExpression):
            it[ExpressionAttributeNames[name]] = ExpressionAttributeValues[val]

    def query(self, KeyConditionExpression, **kw):
        # the store only ever queries one tenant with begins_with("RUN#")
        tenant = KeyConditionExpression._values[0]._values[1]
        return {"Items": [dict(v) for (t, sk), v in self.items.items()
                          if t == tenant and sk.startswith("RUN#")]}


NOW = dt.datetime(2026, 11, 15, 3, 0, tzinfo=dt.timezone.utc)


def test_run_id_is_stable_per_tenant_and_key():
    a = S.run_id_for("t1", "idem-1")
    assert a == S.run_id_for("t1", "idem-1")
    assert a != S.run_id_for("t2", "idem-1") != S.run_id_for("t1", "idem-2")
    with pytest.raises(ValueError):
        S.run_id_for("", "k")


def test_create_run_is_idempotent_and_sets_ttl():
    t = FakeTable()
    item, created = S.create_run(t, tenant="t1", run_id="r1", task="x", target="bradesco", now=NOW)
    assert created and item["status"] == "queued"
    assert item["expires_at"] == int((NOW + dt.timedelta(days=90)).timestamp())
    S.set_status(t, tenant="t1", run_id="r1", status="running")
    again, created2 = S.create_run(t, tenant="t1", run_id="r1", task="other", target="itau", now=NOW)
    assert not created2 and again["status"] == "running" and again["target"] == "bradesco"


def test_status_moves_forward_and_terminal_runs_are_frozen():
    t = FakeTable()
    S.create_run(t, tenant="t1", run_id="r1", task="x", target="b", now=NOW)
    S.set_status(t, tenant="t1", run_id="r1", status="partial", tokens_in=1200, cost_usd=0.42)
    run = S.get_run(t, "t1", "r1")
    assert run["status"] == "partial" and run["tokens_in"] == 1200
    with pytest.raises(ValueError):
        S.set_status(t, tenant="t1", run_id="r1", status="running")
    with pytest.raises(ValueError):
        S.set_status(t, tenant="t1", run_id="missing", status="running")
    with pytest.raises(ValueError):
        S.set_status(t, tenant="t1", run_id="r1", status="bogus")


def test_responses_are_append_only_and_recorded_from_a_runner_result():
    t = FakeTable()
    result = {"run_id": "r1", "phases": {
        "diagnosis": {"risk": {"status": "ok", "position": {"diagnosis": "x", "score": 0.5}},
                      "cso": {"status": "invalid", "errors": ["e"]}},
        "challenge": {"cco": {"status": "ok", "position": {"diagnosis": "y"}}}}}
    assert S.record_result(t, tenant="t1", result=result) == 3
    assert S.record_result(t, tenant="t1", result=result) == 0      # second write is a no-op
    it = t.items[("t1", "RUN#r1#R#diagnosis#risk")]
    assert it["status"] == "ok" and str(it["response"]["position"]["score"]) == "0.5"


def test_list_runs_excludes_responses_and_other_tenants():
    t = FakeTable()
    S.create_run(t, tenant="t1", run_id="r1", task="x", target="b", now=NOW)
    S.create_run(t, tenant="t1", run_id="r2", task="x", target="b", now=NOW + dt.timedelta(hours=1))
    S.create_run(t, tenant="t2", run_id="r9", task="x", target="b", now=NOW)
    S.put_response(t, tenant="t1", run_id="r1", phase="diagnosis", officer="risk", response={"status": "ok"})
    assert [r["run_id"] for r in S.list_runs(t, "t1")] == ["r2", "r1"]
