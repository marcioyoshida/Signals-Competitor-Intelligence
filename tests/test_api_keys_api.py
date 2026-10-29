"""Tenant self-service API-key admin handler (`/api/keys`, `/api/keys/revoke`).

Fail-closed tenant scoping is the property under test: every call must resolve
`tenant_id` from the verified JWT identity, never the request body — and a
caller with no verified tenant gets nothing, not the unscoped legacy mode
other surfaces allow.
"""
from __future__ import annotations

import json

from src.dashboard import api_keys, api_usage, tenant_config
from src.dashboard import api_keys_api as kapi


def _event(*, tenant: str | None, method: str, path: str, body: dict | None = None) -> dict:
    claims = {"sub": "u1"}
    if tenant is not None:
        claims["custom:tenant"] = tenant
    return {
        "requestContext": {"authorizer": {"jwt": {"claims": claims}}, "http": {"method": method}},
        "rawPath": path,
        "body": json.dumps(body or {}),
    }


def _saas(monkeypatch):
    monkeypatch.setattr(tenant_config, "get_tenant_config",
                        lambda t: {"tier": "saas", "modules": ["banking"]})


def test_no_verified_tenant_is_403():
    resp = kapi.lambda_handler(_event(tenant=None, method="GET", path="/api/keys"), None)
    assert resp["statusCode"] == 403


def test_list_is_scoped_to_the_callers_own_tenant(monkeypatch):
    seen = {}

    def fake_list_keys(t):
        seen["tenant"] = t
        return [{"label": "k1"}]

    monkeypatch.setattr(api_keys, "list_keys", fake_list_keys)
    monkeypatch.setattr(api_usage, "get_usage", lambda t: {"period": "2026-09", "tokens_used": 0, "calls": 0})
    resp = kapi.lambda_handler(_event(tenant="acme", method="GET", path="/api/keys"), None)
    assert resp["statusCode"] == 200
    assert seen["tenant"] == "acme"
    body = json.loads(resp["body"])
    assert body["keys"] == [{"label": "k1"}]


def test_create_uses_identity_tenant_not_any_body_field(monkeypatch):
    seen = {}

    def fake_generate(tenant_id, label, scopes, created_by, table=None):
        seen.update(tenant=tenant_id, label=label, scopes=scopes)
        return {"key_id": "kid1", "secret": "sk_onca_xyz", "prefix": "sk_onca_xy",
                "label": label, "scopes": scopes, "created_at": "now"}

    _saas(monkeypatch)
    monkeypatch.setattr(api_keys, "generate_key", fake_generate)
    ev = _event(tenant="acme", method="POST", path="/api/keys",
                body={"label": "prod agent", "scopes": ["ask"], "tenant_id": "attacker-controlled"})
    resp = kapi.lambda_handler(ev, None)
    assert resp["statusCode"] == 200
    assert seen["tenant"] == "acme"  # body's tenant_id is ignored entirely
    body = json.loads(resp["body"])
    assert body["secret"] == "sk_onca_xyz"


def test_create_rejects_bad_scope_as_400(monkeypatch):
    def fake_generate(tenant_id, label, scopes, created_by, table=None):
        raise ValueError("unknown scope(s) ['act']; valid: ('ask',)")

    _saas(monkeypatch)
    monkeypatch.setattr(api_keys, "generate_key", fake_generate)
    ev = _event(tenant="acme", method="POST", path="/api/keys",
                body={"label": "x", "scopes": ["act"]})
    resp = kapi.lambda_handler(ev, None)
    assert resp["statusCode"] == 400


def test_revoke_is_scoped_to_callers_tenant(monkeypatch):
    seen = {}

    def fake_revoke(tenant_id, key_id):
        seen["args"] = (tenant_id, key_id)
        return True

    monkeypatch.setattr(api_keys, "revoke_key", fake_revoke)
    ev = _event(tenant="acme", method="POST", path="/api/keys/revoke", body={"key_id": "kid1"})
    resp = kapi.lambda_handler(ev, None)
    assert resp["statusCode"] == 200
    assert seen["args"] == ("acme", "kid1")
    assert json.loads(resp["body"]) == {"revoked": True}


def test_revoke_missing_key_id_is_400():
    ev = _event(tenant="acme", method="POST", path="/api/keys/revoke", body={})
    resp = kapi.lambda_handler(ev, None)
    assert resp["statusCode"] == 400


# ADR 025: Entry-tier tenants get no Agentic API access, so they cannot mint a key.

def _no_generate(*a, **k):
    raise AssertionError("generate_key must not run for a refused tenant")


def test_entry_tier_cannot_create_a_key(monkeypatch):
    monkeypatch.setattr(tenant_config, "get_tenant_config",
                        lambda t: {"tier": "entry", "modules": ["crypto"]})
    monkeypatch.setattr(api_keys, "generate_key", _no_generate)
    ev = _event(tenant="acme", method="POST", path="/api/keys", body={"label": "bot"})
    resp = kapi.lambda_handler(ev, None)
    assert resp["statusCode"] == 403
    assert "Entry plan" in json.loads(resp["body"])["error"]


def test_unprovisioned_tenant_cannot_create_a_key(monkeypatch):
    monkeypatch.setattr(tenant_config, "get_tenant_config", lambda t: None)
    monkeypatch.setattr(api_keys, "generate_key", _no_generate)
    ev = _event(tenant="acme", method="POST", path="/api/keys", body={"label": "bot"})
    assert kapi.lambda_handler(ev, None)["statusCode"] == 403


def test_entry_tier_can_still_list_and_revoke(monkeypatch):
    """An Entry tenant holding a key from before a downgrade must still be able to see and
    revoke it, so the gate sits on creation only."""
    monkeypatch.setattr(tenant_config, "get_tenant_config",
                        lambda t: {"tier": "entry", "modules": ["crypto"]})
    monkeypatch.setattr(api_keys, "list_keys", lambda t: [{"key_id": "kid1"}])
    monkeypatch.setattr(api_usage, "get_usage", lambda t: {"period": "2026-10", "tokens_used": 0, "calls": 0})
    monkeypatch.setattr(api_keys, "revoke_key", lambda t, k: True)
    get = kapi.lambda_handler(_event(tenant="acme", method="GET", path="/api/keys"), None)
    assert get["statusCode"] == 200
    rev = kapi.lambda_handler(_event(tenant="acme", method="POST", path="/api/keys/revoke",
                                     body={"key_id": "kid1"}), None)
    assert rev["statusCode"] == 200
