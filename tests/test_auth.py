"""Phase C identity extraction (Cognito) — ADR 002 Decision 7."""
from __future__ import annotations

from src.dashboard.auth import Identity, identity_from_event


def _http_api_event(claims):
    return {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}


def test_identity_from_http_api_authorizer():
    ev = _http_api_event({
        "sub": "u-123", "custom:tenant": "acme-bank", "custom:tier": "saas",
        "email": "a@acme.com", "cognito:groups": ["admins", "strategy"],
    })
    i = identity_from_event(ev)
    assert isinstance(i, Identity)
    assert (i.sub, i.tenant, i.tier, i.email) == ("u-123", "acme-bank", "saas", "a@acme.com")
    assert i.groups == ["admins", "strategy"]


def test_identity_supports_rest_style_and_flattened_groups():
    ev = {"requestContext": {"authorizer": {"claims": {
        "sub": "u-9", "tenant": "consorcio-x", "tier": "entry",
        "cognito:groups": "[ops product]",
    }}}}
    i = identity_from_event(ev)
    assert i.tenant == "consorcio-x" and i.tier == "entry"
    assert i.groups == ["ops", "product"]


def test_no_verified_identity_returns_none():
    # legacy origin-secret mode: no authorizer context / no sub → None (never fabricate)
    assert identity_from_event({}) is None
    assert identity_from_event({"requestContext": {}}) is None
    assert identity_from_event(_http_api_event({"email": "x@y.com"})) is None  # no sub
