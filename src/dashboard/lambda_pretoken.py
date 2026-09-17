"""Cognito Pre Token Generation trigger — resolves custom:tenant/custom:tier for a federated
(Google) login via a pre-registered email->tenant mapping.

Ported from Signals-Creator-Radar (Bluefin)'s Google OAuth runbook
(docs/google-oauth-runbook.md there, docs/google-oauth-runbook.md here), adapted for Onça's
closed tenant model. Bluefin is a self-serve consumer product: a first Google login
auto-provisions a Free tenant. Onça is not — `self_sign_up_enabled=False`, tenants are
operator-provisioned, and `custom:tenant` is `mutable=False`. Bluefin found (live, via
CloudWatch on its own equivalent trigger) that Cognito rejects `AdminUpdateUserAttributes` on
an immutable attribute with "Attribute cannot be updated" EVEN WHEN the attribute was never set
on that user at all — immutable means "settable only at AdminCreateUser time," and a federated
user's Cognito record is created internally by Cognito during the OAuth handshake, with no
attribute list Onça ever controls. There is no API call, ever, that can put a value there after
the fact.

The tenant for a federated user therefore never lives on the Cognito user record. It lives in
`OncaFederatedTenantMapTable`, keyed by EMAIL (not a Google `sub` — Onça's operator knows an
invited design partner's email before that person has ever logged in with Google, unlike a
`sub`, which only exists after a first login; see tenant_config.map_federated_email), and is
injected into every token via `claimsOverrideDetails` — a real attribute for password logins, a
claims-only projection for federated ones, both read identically by every downstream
JWT-authorized API as `claims["custom:tenant"]`.

Fail-closed BY OMISSION, not by rejection: an email with no mapping row gets no claims
override, so the token is issued with no `custom:tenant` claim at all — the existing per-tenant
read boundary (feed_api.py) already 403s on that, so there is nothing new to enforce here.
Throwing from this trigger instead would fail the ENTIRE /oauth2/token exchange with no error
surfaced to the browser (confirmed as Bluefin's actual failure mode the first time it hit this
class of bug) — letting the login "succeed" into an already-fail-closed API is strictly safer
than trying to reject it here.

Also fires on every native password login, since a Pre Token Generation trigger attaches to
the whole pool, not to a specific identity provider. Those users already carry a real
`custom:tenant`/`custom:tier` attribute set at `AdminCreateUser` time — but it must still be
explicitly re-asserted via `claimsOverrideDetails` here, NOT left as a no-op: confirmed live
(ADR 027 #126) that Cognito's Hosted UI OAuth Authorization Code flow (this pool's
`PreTokenGenerationConfig.LambdaVersion = V1_0`) omits custom attributes from the ID token
unless a trigger explicitly re-asserts them, even though the SAME user's `AdminInitiateAuth`
token includes them fine. Before this was discovered, EVERY password-login tenant's ID token
was silently missing `custom:tenant`, which would 403 at the `/api/feed` read boundary — this
class of bug had gone unnoticed because Google logins (which always go through the federated
`claimsOverrideDetails` branch below) were the only OAuth-flow logins tested end-to-end.
"""
from __future__ import annotations

import os

FEDERATED_TABLE = os.environ.get("ONCA_FEDERATED_MAP_TABLE", "")


def lambda_handler(event, context):
    attrs = event.get("request", {}).get("userAttributes", {}) or {}
    if attrs.get("custom:tenant"):
        # Password user — the attribute is real, set at AdminCreateUser time. It shows up
        # fine in a native InitiateAuth/AdminInitiateAuth token, but Cognito's Hosted UI
        # OAuth Authorization Code flow (V1_0 Pre Token Generation) omits custom attributes
        # from the ID token unless a trigger explicitly re-asserts them via
        # claimsOverrideDetails — confirmed live (ADR 027 #126 QA persona round-trip):
        # AdminInitiateAuth returned custom:tenant/custom:tier, the Hosted UI code-grant
        # token for the SAME user did not. Re-assert explicitly rather than no-op.
        event.setdefault("response", {})
        event["response"]["claimsOverrideDetails"] = {
            "claimsToAddOrOverride": {
                "custom:tenant": attrs["custom:tenant"],
                "custom:tier": attrs.get("custom:tier", "saas"),
            }
        }
        return event

    email = (attrs.get("email") or "").strip().lower()
    if not email or not FEDERATED_TABLE:
        return event

    import boto3

    item = boto3.client("dynamodb").get_item(
        TableName=FEDERATED_TABLE, Key={"email": {"S": email}}
    ).get("Item")
    if not item:
        print(f"[pretoken] no federated tenant mapping for {email}; issuing token with no tenant claim")
        return event

    event.setdefault("response", {})
    event["response"]["claimsOverrideDetails"] = {
        "claimsToAddOrOverride": {
            "custom:tenant": item["tenant_id"]["S"],
            "custom:tier": item.get("tier", {}).get("S", "saas"),
        }
    }
    return event
