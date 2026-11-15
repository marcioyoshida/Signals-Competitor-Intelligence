"""#322 board Lambda tasks: admit → pack → run, offline (tables, S3, feed and model faked)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.board import handler as H
from src.board import pack as P
from test_board_pack import feed as small_feed
from test_board_store import FakeTable


class FakeS3:
    def __init__(self):
        self.store = {}

    def put_object(self, Bucket, Key, Body, ContentType=None, IfNoneMatch=None):
        if IfNoneMatch and Key in self.store:
            e = Exception("exists")
            e.response = {"Error": {"Code": "PreconditionFailed"}}
            raise e
        self.store[Key] = Body

    def get_object(self, Bucket, Key):
        body = self.store[Key]
        return {"Body": type("B", (), {"read": lambda self: body})()}


@pytest.fixture
def env(monkeypatch):
    t, s3 = FakeTable(), FakeS3()
    monkeypatch.setenv("ONCA_BOARD_BUCKET", "packs")
    monkeypatch.setattr(H, "_table", lambda: t)
    monkeypatch.setattr(H, "_s3", lambda: s3)
    monkeypatch.setattr(H, "_scoped_feed", lambda tenant: small_feed())
    monkeypatch.setattr(H, "_client_connection", lambda tenant: None)
    rows = {"paying": {"tenant_id": "paying"}, "trial": {"tenant_id": "trial"}}
    monkeypatch.setattr(H, "_raw_tenant_row", lambda tenant: rows.get(tenant))
    cfgs = {"paying": {"tenant_id": "paying", "modules": ["banking"], "billing": {"state": "active"}},
            "trial": {"tenant_id": "trial", "modules": ["banking"], "billing": {"state": "trial"}}}
    import src.dashboard.tenant_config as tc
    monkeypatch.setattr(tc, "get_tenant_config", lambda tenant, **kw: cfgs.get(tenant))
    return t, s3


def req(tenant="paying", key="k1", target="alfa"):
    return {"action": "admit", "tenant": tenant, "idempotency_key": key, "target": target,
            "task": "Rebaixamento de rating do Banco Alfa"}


def test_admit_refuses_trial_and_records_it(env):
    t, _ = env
    out = H.lambda_handler(req("trial"), None)
    assert out == {"allowed": False, "reason": "not_paying:trial", "run_id": out["run_id"]}
    assert t.items[("trial", f"RUN#{out['run_id']}")]["status"] == "refused"


def test_admit_paying_then_retry_is_duplicate(env):
    a = H.lambda_handler(req(), None)
    assert a["allowed"] and a["reason"] == "paying" and a["token_cap"] > 0
    b = H.lambda_handler(req(), None)
    assert not b["allowed"] and b["reason"] == "duplicate" and b["run_id"] == a["run_id"]


def test_bad_request(env):
    assert H.lambda_handler({**req(), "task": " "}, None)["reason"] == "bad_request"
    with pytest.raises(ValueError):
        H.lambda_handler({"action": "nope"}, None)


def test_pack_stores_a_sealed_pack_and_out_of_scope_target_fails_visibly(env):
    t, s3 = env
    a = H.lambda_handler(req(), None)
    p = H.lambda_handler({"action": "pack", **a}, None)
    assert p["ok"] and P.pack_key(p["pack_sha"]) in s3.store
    assert t.items[("paying", f"RUN#{a['run_id']}")]["pack_sha"] == p["pack_sha"]
    a2 = H.lambda_handler(req(key="k2", target="ghost"), None)
    bad = H.lambda_handler({"action": "pack", **a2}, None)
    assert not bad["ok"] and t.items[("paying", f"RUN#{a2['run_id']}")]["status"] == "failed"


def test_run_meters_persists_and_finishes(env, monkeypatch):
    t, _ = env
    from src.synth import bedrock_llm, client_model
    from src.dashboard import api_usage
    metered = []
    monkeypatch.setattr(client_model, "resolve_target", lambda tenant, loader=None: None)
    monkeypatch.setattr(client_model, "tenant_loader", lambda: None)
    monkeypatch.setattr(api_usage, "record_usage", lambda tenant, tokens, **kw: metered.append(tokens))

    def fake_converse(prompt, *, system, max_tokens, usage_out, target, context, guardrail, guard_text):
        assert context.startswith("PACOTE DE CONTEXTO") and target is None
        usage_out.update(input_tokens=1000, output_tokens=200)
        if system.startswith("Você é o secretário"):
            return json.dumps({"summary": "s", "residual_regulatory_risk": "r", "monitoring_triggers": [],
                               "options": [{"id": f"o{i}", "description": "d", "customer_impact": "low",
                                            "citations": ["f-km1-alfa"]} for i in (1, 2)]})
        off = prompt.rsplit('"officer": "', 1)[1].split('"')[0]
        return json.dumps({"officer": off, "diagnosis": "x", "constraints": [], "options": [],
                           "citations": [{"pack_fact_id": "f-km1-alfa"}], "tool_call_summary": []})
    monkeypatch.setattr(bedrock_llm, "converse", fake_converse)

    a = H.lambda_handler(req(), None)
    p = H.lambda_handler({"action": "pack", **a}, None)
    out = H.lambda_handler({"action": "run", **p}, None)
    run = t.items[("paying", f"RUN#{a['run_id']}")]
    assert out["status"] == "complete", run.get("gaps")
    assert out["calls"] == 5 + 3 + 3 + 1
    run = t.items[("paying", f"RUN#{a['run_id']}")]
    assert run["status"] == "complete" and run["usage"]["tokens_in"] == 12_000
    assert metered == [14_400]
    assert sum(1 for (tn, sk) in t.items if sk.startswith(f"RUN#{a['run_id']}#R#")) == 12
    # a late catch never overwrites a finished run
    assert H.lambda_handler({"action": "fail", "tenant": "paying", "run_id": a["run_id"]}, None)
    assert t.items[("paying", f"RUN#{a['run_id']}")]["status"] == "complete"


def test_run_stops_at_the_token_cap(env, monkeypatch):
    from src.synth import bedrock_llm, client_model
    from src.dashboard import api_usage
    monkeypatch.setattr(client_model, "resolve_target", lambda tenant, loader=None: None)
    monkeypatch.setattr(client_model, "tenant_loader", lambda: None)
    monkeypatch.setattr(api_usage, "record_usage", lambda *a, **k: None)

    def big(prompt, *, usage_out, **kw):
        usage_out.update(input_tokens=50_000, output_tokens=0)
        off = prompt.rsplit('"officer": "', 1)[1].split('"')[0]
        return json.dumps({"officer": off, "diagnosis": "x", "citations": [{"pack_fact_id": "f-km1-alfa"}]})
    monkeypatch.setattr(bedrock_llm, "converse", big)
    a = H.lambda_handler(req(), None)
    p = H.lambda_handler({"action": "pack", **a, "token_cap": 100_000}, None)
    out = H.lambda_handler({"action": "run", **p}, None)
    assert out["status"] == "partial" and out["calls"] == 5   # stopped after diagnosis


def test_real_client_connection_is_none_when_byok_is_not_configured(monkeypatch):
    monkeypatch.delenv("ONCA_MODEL_KEYS_TABLE", raising=False)
    monkeypatch.delenv("ONCA_MODEL_KEY_KMS", raising=False)
    assert H._client_connection("t1") is None


def test_every_lazy_import_in_the_handler_resolves():
    import importlib
    import re
    src = open(H.__file__, encoding="utf-8").read()
    for mod, names in re.findall(r"from (src\.[\w.]+) import ([\w, ]+)", src):
        m = importlib.import_module(mod)
        for n in names.split(","):
            n = n.split(" as ")[0].strip()
            assert hasattr(m, n) or importlib.util.find_spec(f"{mod}.{n}"), f"{mod}.{n}"
