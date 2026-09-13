"""Per-tenant scoped feed (`GET /api/feed`, ADR 016) — including the industry
Cognito group fallback (no tenant_config row needed) added alongside the onssa.org
group wiring."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import feed_api


def _ev(claims):
    return {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}


def _feed():
    return {
        "entity_attrs": {
            "itau": {"industries": ["banking"]},
            "nubank": {"industries": ["fintech"]},
        },
        "groups": {},
    }


class _FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeS3:
    def __init__(self, feed: dict) -> None:
        self._feed = feed

    def get_object(self, Bucket, Key):
        return {"Body": _FakeBody(json.dumps(self._feed).encode())}


def _patch_s3(monkeypatch, feed):
    import boto3

    monkeypatch.setattr(boto3, "client", lambda name: _FakeS3(feed))


def test_forbids_without_identity():
    r = feed_api.lambda_handler({}, None)
    assert r["statusCode"] == 403


def test_tenant_with_modules_scopes_feed(monkeypatch):
    import src.dashboard.tenant_config as tc

    monkeypatch.setattr(tc, "get_tenant_config", lambda t: {"tier": "saas", "modules": ["banking"]})
    monkeypatch.setenv("ONCA_SITE_BUCKET", "b")
    _patch_s3(monkeypatch, _feed())
    ev = _ev({"sub": "u", "custom:tenant": "acme"})
    r = feed_api.lambda_handler(ev, None)
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert body["tenant"] == "acme"
    assert body["tier"] == "saas"


def test_unprovisioned_tenant_fails_closed(monkeypatch):
    import src.dashboard.tenant_config as tc

    monkeypatch.setattr(tc, "get_tenant_config", lambda t: None)
    ev = _ev({"sub": "u", "custom:tenant": "ghost-tenant"})
    r = feed_api.lambda_handler(ev, None)
    assert r["statusCode"] == 403


def test_industry_group_scopes_feed_without_a_tenant_config_row(monkeypatch):
    """The onssa.org group-wiring path: no custom:tenant at all, just a Cognito
    group named after a real industry — must still get a scoped 200, not 403."""
    monkeypatch.setenv("ONCA_SITE_BUCKET", "b")
    _patch_s3(monkeypatch, _feed())
    ev = _ev({"sub": "u", "cognito:groups": ["banking"]})
    r = feed_api.lambda_handler(ev, None)
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert body["tenant"] is None
    assert body["tier"] == "group"


def test_tenant_takes_precedence_over_industry_group(monkeypatch):
    """A real provisioned tenant's modules win even if the same identity also
    carries an (unrelated) industry group claim."""
    import src.dashboard.tenant_config as tc

    monkeypatch.setattr(tc, "get_tenant_config", lambda t: {"tier": "saas", "modules": ["fintech"]})
    monkeypatch.setenv("ONCA_SITE_BUCKET", "b")
    _patch_s3(monkeypatch, _feed())
    ev = _ev({"sub": "u", "custom:tenant": "acme", "cognito:groups": ["banking"]})
    r = feed_api.lambda_handler(ev, None)
    body = json.loads(r["body"])
    assert body["tenant"] == "acme"
    assert body["tier"] == "saas"


def test_no_tenant_and_no_industry_group_fails_closed():
    ev = _ev({"sub": "u", "cognito:groups": ["not-a-real-industry", "strategy"]})
    r = feed_api.lambda_handler(ev, None)
    assert r["statusCode"] == 403
