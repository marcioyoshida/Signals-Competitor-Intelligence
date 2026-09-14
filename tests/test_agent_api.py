"""Agentic API handler (`/api/v1/agent/ask`) — API-key auth, tenant scoping,
usage metering. Bedrock/S3/DynamoDB are all monkeypatched (see agent_ask's own
tests for the same DI-first convention this mirrors)."""
from __future__ import annotations

import json

import pytest

from src.dashboard import agent_api as aapi
from src.dashboard import api_keys, api_usage, tenant_config


def _event(auth_header: str | None, body: dict | None = None) -> dict:
    headers = {}
    if auth_header is not None:
        headers["authorization"] = auth_header
    return {"headers": headers, "body": json.dumps(body or {})}


def test_missing_bearer_is_401(monkeypatch):
    resp = aapi.lambda_handler(_event(None), None)
    assert resp["statusCode"] == 401


def test_unknown_key_is_401(monkeypatch):
    monkeypatch.setattr(api_keys, "lookup_key", lambda raw, table=None: None)
    resp = aapi.lambda_handler(_event("Bearer sk_onca_bogus"), None)
    assert resp["statusCode"] == 401


def test_key_without_ask_scope_is_403(monkeypatch):
    monkeypatch.setattr(api_keys, "lookup_key",
                         lambda raw, table=None: {"tenant_id": "acme", "scopes": [], "key_hash": "h"})
    resp = aapi.lambda_handler(_event("Bearer sk_onca_x"), None)
    assert resp["statusCode"] == 403


def test_tenant_with_no_entitlement_is_403(monkeypatch):
    monkeypatch.setattr(api_keys, "lookup_key",
                         lambda raw, table=None: {"tenant_id": "acme", "scopes": ["ask"], "key_hash": "h"})
    monkeypatch.setattr(tenant_config, "get_tenant_config", lambda t: None)
    resp = aapi.lambda_handler(_event("Bearer sk_onca_x", {"q": "hi"}), None)
    assert resp["statusCode"] == 403


def test_missing_question_is_400(monkeypatch):
    monkeypatch.setattr(api_keys, "lookup_key",
                         lambda raw, table=None: {"tenant_id": "acme", "scopes": ["ask"], "key_hash": "h"})
    monkeypatch.setattr(tenant_config, "get_tenant_config",
                         lambda t: {"tier": "saas", "modules": ["banking"]})
    resp = aapi.lambda_handler(_event("Bearer sk_onca_x", {}), None)
    assert resp["statusCode"] == 400


def test_happy_path_records_usage_and_touches_key(monkeypatch):
    monkeypatch.setenv("ONCA_SITE_BUCKET", "bucket")
    row = {"tenant_id": "acme", "scopes": ["ask"], "key_hash": "hash123"}
    monkeypatch.setattr(api_keys, "lookup_key", lambda raw, table=None: row)
    monkeypatch.setattr(tenant_config, "get_tenant_config",
                         lambda t: {"tier": "saas", "modules": ["banking"]})
    monkeypatch.setattr(aapi, "_load_feed", lambda b: {"feed": []})

    def fake_answer(q, *, feed, scope, converser, kb_retrieve, modules, persona):
        # Exercise the metered converser exactly like the real `answer()` would,
        # so the usage-capture wiring is actually verified end to end.
        converser("prompt", system="sys", max_tokens=100)
        return {"answer": "grounded reply", "refused": False, "grounded": True, "citations": []}

    monkeypatch.setattr(aapi, "answer", fake_answer)

    def fake_converse(prompt, *, system=None, max_tokens=800, usage_out=None):
        if usage_out is not None:
            usage_out["input_tokens"] = 12
            usage_out["output_tokens"] = 34
        return "grounded reply"

    import src.synth.bedrock_llm as bl
    monkeypatch.setattr(bl, "converse", fake_converse)

    recorded = {}
    monkeypatch.setattr(api_usage, "record_usage",
                         lambda tenant_id, tokens, table=None: recorded.update(tenant=tenant_id, tokens=tokens))
    touched = {}
    monkeypatch.setattr(api_keys, "touch_last_used",
                         lambda key_hash, table=None: touched.update(hash=key_hash))

    resp = aapi.lambda_handler(_event("Bearer sk_onca_x", {"q": "who is the market leader?"}), None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["answer"] == "grounded reply"
    assert body["usage"] == {"input_tokens": 12, "output_tokens": 34, "total_tokens": 46}
    assert recorded == {"tenant": "acme", "tokens": 46}
    assert touched == {"hash": "hash123"}
