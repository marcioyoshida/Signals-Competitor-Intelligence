"""Cognito Pre Token Generation trigger — resolves custom:tenant/custom:tier for a Google
login via a pre-registered email->tenant mapping. custom:tenant is mutable=False and Cognito
rejects AdminUpdateUserAttributes on it even when it was never set (confirmed live in the
Bluefin fork this pattern is ported from) — a federated user's tenant is injected into the
token via claimsOverrideDetails instead. A password login's custom:tenant is a REAL attribute,
but must still be explicitly re-asserted via claimsOverrideDetails, not left as a no-op — see
lambda_pretoken.py's module docstring for the live-confirmed Hosted UI OAuth gotcha (ADR 027
#126) this test guards against regressing."""
from __future__ import annotations

from unittest import mock

from src.dashboard import lambda_pretoken


def _event(attrs):
    return {"request": {"userAttributes": attrs}, "response": {}}


def test_password_login_reasserts_its_own_tenant_and_tier_claims():
    """Not a no-op: Cognito's Hosted UI OAuth code-grant flow omits custom attributes from the
    ID token unless the trigger explicitly re-adds them, even though the same user's
    AdminInitiateAuth token includes them fine (confirmed live 2026-09-16) — no DynamoDB lookup
    needed since the values are already on the incoming password-login attributes."""
    ev = _event({"email": "a@b.com", "custom:tenant": "t-existing", "custom:tier": "saas"})
    with mock.patch("boto3.client") as bc:
        out = lambda_pretoken.lambda_handler(ev, None)
    bc.assert_not_called()
    override = out["response"]["claimsOverrideDetails"]["claimsToAddOrOverride"]
    assert override == {"custom:tenant": "t-existing", "custom:tier": "saas"}


def test_password_login_defaults_tier_to_saas_if_somehow_missing():
    ev = _event({"email": "a@b.com", "custom:tenant": "t-existing"})
    with mock.patch("boto3.client") as bc:
        out = lambda_pretoken.lambda_handler(ev, None)
    bc.assert_not_called()
    override = out["response"]["claimsOverrideDetails"]["claimsToAddOrOverride"]
    assert override == {"custom:tenant": "t-existing", "custom:tier": "saas"}


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
