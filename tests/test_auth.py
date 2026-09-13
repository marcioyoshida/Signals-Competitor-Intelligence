"""Phase C identity extraction (Cognito) — ADR 002 Decision 7."""
from __future__ import annotations

from src.dashboard.auth import Identity, identity_from_event, origin_secret_ok


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


# --- origin gate (WAF Phase 0) -------------------------------------------------------
# The second of two gates. The first is CloudFront OAC: every Lambda function URL is
# AuthType AWS_IAM, so an unsigned direct call to the *.lambda-url.*.on.aws hostname
# never reaches a handler. This one checks the secret CloudFront injects as a custom
# header, and it must FAIL CLOSED — the old `if secret and header != secret` form
# turned a missing environment variable into an open endpoint.
def _ev(header=None):
    return {"headers": {} if header is None else {"x-onca-origin": header}}


def test_origin_gate_accepts_the_matching_secret(monkeypatch):
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", "s3cr3t")
    assert origin_secret_ok(_ev("s3cr3t")) is True
    assert origin_secret_ok({"headers": {"X-Onca-Origin": "s3cr3t"}}) is True  # case-insensitive


def test_origin_gate_rejects_a_wrong_or_missing_header(monkeypatch):
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", "s3cr3t")
    assert origin_secret_ok(_ev("wrong")) is False
    assert origin_secret_ok(_ev()) is False
    assert origin_secret_ok({}) is False
    assert origin_secret_ok(None) is False


def test_origin_gate_fails_closed_when_the_secret_is_unset(monkeypatch):
    """The regression this whole change exists for: no secret must DENY, not disable."""
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    assert origin_secret_ok(_ev("anything")) is False
    assert origin_secret_ok(_ev()) is False

    monkeypatch.setenv("ONCA_ORIGIN_SECRET", "")  # set-but-empty is still unconfigured
    assert origin_secret_ok(_ev("")) is False
    assert origin_secret_ok(_ev("anything")) is False
