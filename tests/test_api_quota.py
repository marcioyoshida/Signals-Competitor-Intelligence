"""#305 / ADR 025: included API quota (1M tokens per module per month) + overage marking."""
from __future__ import annotations

import json
import threading

from src.dashboard import agent_api as aapi
from src.dashboard import api_keys, api_usage, tenant_config


def test_quota_defaults_per_module_and_honours_an_override():
    assert api_usage.QUOTA_PER_MODULE == 1_000_000
    assert api_usage.quota_for({"modules": ["banking", "fintech"]}) == 2_000_000
    assert api_usage.quota_for({"modules": ["banking"], "api_quota_tokens": 50_000}) == 50_000
    assert api_usage.quota_for(None) == 0


def test_overage_slice():
    assert api_usage.overage_slice(900, 100, 1000) == 0          # still inside
    assert api_usage.overage_slice(1050, 100, 1000) == 50        # this call crossed the line
    assert api_usage.overage_slice(1300, 100, 1000) == 100       # already past: all of it


class _Usage:
    """Atomic-ADD fake of onca-api-usage (one row per tenant+period)."""

    def __init__(self):
        self.rows, self.lock = {}, threading.Lock()

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ReturnValues=None):
        with self.lock:
            row = self.rows.setdefault((Key["tenant_id"], Key["period"]), {})
            v = ExpressionAttributeValues
            if ":t" in v:
                row["tokens_used"] = row.get("tokens_used", 0) + v[":t"]
                row["calls"] = row.get("calls", 0) + 1
            if ":o" in v:
                row["overage_tokens"] = row.get("overage_tokens", 0) + v[":o"]
                row["overage"] = True
            return {"Attributes": {"tokens_used": row["tokens_used"]}} if ReturnValues else {}

    def get_item(self, Key):
        r = self.rows.get((Key["tenant_id"], Key["period"]))
        return {"Item": dict(r)} if r else {}


def test_overage_is_marked_only_above_the_quota_and_adds_up_under_concurrency():
    t = _Usage()
    first = api_usage.record_usage("acme", 600, quota=1000, table=t)
    assert first["remaining"] == 400 and first["overage_tokens"] == 0
    row = next(iter(t.rows.values()))
    assert "overage" not in row
    threads = [threading.Thread(target=api_usage.record_usage, args=("acme", 100),
                                kwargs={"quota": 1000, "table": t}) for _ in range(10)]
    [th.start() for th in threads]
    [th.join() for th in threads]
    assert row["tokens_used"] == 1600 and row["overage"] is True
    assert row["overage_tokens"] == 600                       # exactly the tokens past 1000
    assert api_usage.get_usage("acme", period=next(iter(t.rows))[1], table=t)["overage_tokens"] == 600


def test_quota_headers():
    h = api_usage.quota_headers(1000, 1600, "2026-11")
    assert h == {"X-Onca-Quota-Limit": "1000", "X-Onca-Quota-Remaining": "0", "X-Onca-Quota-Period": "2026-11"}


def test_api_response_carries_headers_and_is_served_past_the_quota(monkeypatch):
    monkeypatch.setenv("ONCA_SITE_BUCKET", "bucket")
    monkeypatch.setattr(api_keys, "lookup_key",
                        lambda raw, table=None: {"tenant_id": "acme", "scopes": ["ask"], "key_hash": "h"})
    monkeypatch.setattr(api_keys, "touch_last_used", lambda h, table=None: None)
    monkeypatch.setattr(tenant_config, "get_tenant_config",
                        lambda t: {"tier": "saas", "modules": ["banking"], "api_quota_tokens": 100})
    monkeypatch.setattr(aapi, "_load_feed", lambda b: {"feed": []})
    import src.synth.bedrock_llm as bl

    def fake_converse(prompt, *, usage_out=None, **kw):
        usage_out.update(input_tokens=120, output_tokens=30)
        return "x"
    monkeypatch.setattr(bl, "converse", fake_converse)
    monkeypatch.setattr(aapi, "answer", lambda q, converser=None, **kw: (converser("p"), {"answer": "ok"})[1])
    t = _Usage()
    real = api_usage.record_usage
    monkeypatch.setattr(api_usage, "record_usage", lambda tid, tok, quota=None, table=None: real(tid, tok, quota=quota, table=t))
    r = aapi.lambda_handler({"headers": {"authorization": "Bearer sk_onca_x"}, "body": json.dumps({"q": "Itaú?"})}, None)
    assert r["statusCode"] == 200                              # over quota: still answered
    assert r["headers"]["X-Onca-Quota-Limit"] == "100" and r["headers"]["X-Onca-Quota-Remaining"] == "0"
    body = json.loads(r["body"])
    assert body["usage"]["overage_tokens"] == 50 and body["quota"]["used_tokens"] == 150
    assert next(iter(t.rows.values()))["overage"] is True


def test_get_tenant_config_carries_the_quota_override():
    """Live 2026-11-02: get_tenant_config dropped api_quota_tokens, so a 100-token test quota
    read as the 2M module default."""
    class _T:
        items = {}

        def get_item(self, Key):
            return {"Item": self.items.get(Key["tenant_id"])} if Key["tenant_id"] in self.items else {}

        def put_item(self, Item):
            self.items[Item["tenant_id"]] = dict(Item)
    t = _T()
    t.put_item({"tenant_id": "acme", "tier": "saas", "modules": ["banking", "fintech"], "api_quota_tokens": 100})
    cfg = tenant_config.get_tenant_config("acme", table=t)
    assert api_usage.quota_for(cfg) == 100
    tenant_config.put_tenant_config("acme", "saas", ["banking"], table=t)          # operator re-put keeps it
    assert api_usage.quota_for(tenant_config.get_tenant_config("acme", table=t)) == 100
