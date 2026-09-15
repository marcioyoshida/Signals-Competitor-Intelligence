"""POST /api/register — lazy Entry-tier self-registration (see
docs/google-oauth-runbook.md, src.dashboard.self_register)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import self_register


def _ev(claims, body=None):
    ev = {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}
    if body is not None:
        ev["body"] = json.dumps(body)
    return ev


def test_rejects_without_identity():
    r = self_register.lambda_handler({}, None)
    assert r["statusCode"] == 401


def test_rejects_already_provisioned_identity():
    ev = _ev({"sub": "u", "custom:tenant": "acme"}, {"industries": ["crypto"]})
    r = self_register.lambda_handler(ev, None)
    assert r["statusCode"] == 409


def test_rejects_identity_with_no_email():
    ev = _ev({"sub": "u"}, {"industries": ["crypto"]})
    r = self_register.lambda_handler(ev, None)
    assert r["statusCode"] == 400


def test_creates_entry_tenant_for_unprovisioned_google_identity(monkeypatch):
    import src.dashboard.tenant_config as tc

    calls = {}

    def _fake(email, industries, **kw):
        calls["email"], calls["industries"] = email, industries
        return {"tenant_id": "entry-ops-ab12", "tier": "entry", "modules": ["crypto"], "plane": "portal"}

    monkeypatch.setattr(tc, "self_register_entry_tenant", _fake)
    ev = _ev({"sub": "u", "email": "ops@example.com"}, {"industries": ["crypto"]})
    r = self_register.lambda_handler(ev, None)
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert body["tenant_id"] == "entry-ops-ab12"
    assert calls["email"] == "ops@example.com"
    assert calls["industries"] == ["crypto"]


def test_rejects_bad_industry_choice(monkeypatch):
    import src.dashboard.tenant_config as tc

    def _fake(email, industries, **kw):
        raise ValueError("pick at least one entry-tier industry: []")

    monkeypatch.setattr(tc, "self_register_entry_tenant", _fake)
    ev = _ev({"sub": "u", "email": "ops@example.com"}, {"industries": ["banking"]})
    r = self_register.lambda_handler(ev, None)
    assert r["statusCode"] == 400
    assert "choices" in json.loads(r["body"])


def test_missing_body_is_handled_not_a_crash():
    ev = _ev({"sub": "u", "email": "ops@example.com"})  # no body key at all
    r = self_register.lambda_handler(ev, None)
    # industries defaults to [] -> self_register_entry_tenant's own "pick at least
    # one" ValueError fires before any DynamoDB call, so this needs no mocking.
    assert r["statusCode"] == 400
