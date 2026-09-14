"""Cognito Pre Token Generation trigger — resolves custom:tenant/custom:tier for a Google
login via a pre-registered email->tenant mapping. custom:tenant is mutable=False and Cognito
rejects AdminUpdateUserAttributes on it even when it was never set (confirmed live in the
Bluefin fork this pattern is ported from) — a federated user's tenant is injected into the
token via claimsOverrideDetails instead, and must be a fast no-op for a password login (which
already has a real custom:tenant attribute)."""
from __future__ import annotations

from unittest import mock

from src.dashboard import lambda_pretoken


def _event(attrs):
    return {"request": {"userAttributes": attrs}, "response": {}}


def test_password_login_is_a_pure_noop():
    ev = _event({"email": "a@b.com", "custom:tenant": "t-existing"})
    with mock.patch("boto3.client") as bc:
        out = lambda_pretoken.lambda_handler(ev, None)
    bc.assert_not_called()
    assert out is ev
    assert "claimsOverrideDetails" not in out["response"]


def test_mapped_google_email_injects_tenant_and_tier_claims():
    ev = _event({"email": "Ops@Acme.Example"})
    ddb = mock.Mock()
    ddb.get_item.return_value = {
        "Item": {"email": {"S": "ops@acme.example"},
                  "tenant_id": {"S": "acme"}, "tier": {"S": "saas"}}
    }
    with mock.patch("boto3.client", return_value=ddb), \
         mock.patch.object(lambda_pretoken, "FEDERATED_TABLE", "onca-federated-tenant-map"):
        out = lambda_pretoken.lambda_handler(ev, None)

    # looked up by the LOWERCASED email — Google's claim casing must not create a miss
    ddb.get_item.assert_called_once_with(
        TableName="onca-federated-tenant-map", Key={"email": {"S": "ops@acme.example"}})
    override = out["response"]["claimsOverrideDetails"]["claimsToAddOrOverride"]
    assert override == {"custom:tenant": "acme", "custom:tier": "saas"}


def test_unmapped_google_email_fails_closed_by_omission_not_rejection():
    """No mapping row -> no claims override at all, NOT an exception. Throwing here would fail
    the entire /oauth2/token exchange with no error surfaced to the browser (the documented
    Bluefin failure mode) — the existing per-tenant read boundary already 403s on a token with
    no custom:tenant, so letting the login "succeed" into that is strictly safer."""
    ev = _event({"email": "stranger@gmail.com"})
    ddb = mock.Mock()
    ddb.get_item.return_value = {}
    with mock.patch("boto3.client", return_value=ddb), \
         mock.patch.object(lambda_pretoken, "FEDERATED_TABLE", "onca-federated-tenant-map"):
        out = lambda_pretoken.lambda_handler(ev, None)
    assert "claimsOverrideDetails" not in out["response"]


def test_no_email_at_all_does_not_crash():
    ev = _event({})
    with mock.patch("boto3.client") as bc, \
         mock.patch.object(lambda_pretoken, "FEDERATED_TABLE", "onca-federated-tenant-map"):
        out = lambda_pretoken.lambda_handler(ev, None)
    bc.assert_not_called()
    assert "claimsOverrideDetails" not in out["response"]


def test_federated_table_not_configured_is_a_noop_not_a_crash():
    """A password-only deploy (Google never wired) leaves ONCA_FEDERATED_MAP_TABLE unset — the
    trigger itself only exists once google_idp is configured (infra/app.py), but guard here too
    rather than let a misconfigured env blow up every login."""
    ev = _event({"email": "someone@gmail.com"})
    with mock.patch("boto3.client") as bc, \
         mock.patch.object(lambda_pretoken, "FEDERATED_TABLE", ""):
        out = lambda_pretoken.lambda_handler(ev, None)
    bc.assert_not_called()
    assert "claimsOverrideDetails" not in out["response"]
