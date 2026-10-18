"""#262 P1: the identity block is a helper (infra/identity.py) building the ORIGINAL pool with
byte-identical logical ids and a side-by-side V2 pool whose email is MUTABLE.

Synthesizes the helper into a bare stack (seconds, no AWS calls): construct ids are created
directly in the stack scope, so the logical ids here are the same ones OncaPrototypeStack emits.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra"))

cdk = pytest.importorskip("aws_cdk")
from aws_cdk import assertions  # noqa: E402

import identity  # noqa: E402

APP_SRC = (ROOT / "infra" / "app.py").read_text(encoding="utf-8")
# stack output OncaWarroomClientV2Id (#262 P1 deploy) — the site hard-codes the public SPA client id
V2_WARROOM_CLIENT_ID = "4docsvvsocsujjqtj3aofduio3"

# Every Cognito logical id of the live pool as synthesized BEFORE the refactor (ccc1e2c),
# domains excluded (they stay in app.py). Changing any of these would replace a live resource.
OLD_IDS = {
    "OncaUserPool4190A2E4": "AWS::Cognito::UserPool",
    "OncaUserPoolOncaWarroomClient9847E8B1": "AWS::Cognito::UserPoolClient",
    "OncaUserPoolOncaOAuthFacadeClientB9FEF346": "AWS::Cognito::UserPoolClient",
    "OncaGoogleIdp9F9682B2": "AWS::Cognito::UserPoolIdentityProvider",
    "OncaAppleIdp7E8E4983": "AWS::Cognito::UserPoolIdentityProvider",
    **{f"OncaGroup{g.title().replace('-', '')}": "AWS::Cognito::UserPoolGroup"
       for g in identity.INDUSTRY_GROUPS},
    "OncaGroupOperator": "AWS::Cognito::UserPoolGroup",
    "OncaDemoUserAcquiring": "AWS::Cognito::UserPoolUser",
    "OncaDemoUserFintech": "AWS::Cognito::UserPoolUser",
    "OncaDemoUserInsurance": "AWS::Cognito::UserPoolUser",
    "OncaDemoUserWealthmanagement": "AWS::Cognito::UserPoolUser",
    "OncaFleetHealthCheckUser": "AWS::Cognito::UserPoolUser",
    "OncaQaUserEntry": "AWS::Cognito::UserPoolUser",
    "OncaQaUserSovereign": "AWS::Cognito::UserPoolUser",
    "OncaQaUserAdminOperatorGroup": "AWS::Cognito::UserPoolUserToGroupAttachment",
}

CONTEXT = {
    "google_client_id": "test.apps.googleusercontent.com",
    "apple_services_id": "org.example.signin", "apple_team_id": "TEAM", "apple_key_id": "KEY",
}


@pytest.fixture(scope="module")
def resources():
    app = cdk.App(context=CONTEXT)
    stack = cdk.Stack(app, "T", env=cdk.Environment(account="111111111111", region="us-east-1"))
    old = identity.build_identity_pool(stack, "", distribution_domain="d1.cloudfront.net")
    v2 = identity.build_identity_pool(stack, "V2", distribution_domain="d1.cloudfront.net",
                                      email_mutable=True, deletion_protection=True)
    assert old.google_idp and old.apple_idp and v2.google_idp and v2.apple_idp
    return assertions.Template.from_stack(stack).to_json()["Resources"]


def _pool(resources, prefix):
    (lid,) = [k for k, v in resources.items()
              if v["Type"] == "AWS::Cognito::UserPool" and k[: -8] == prefix]
    return lid, resources[lid]


def _email(pool):
    return next(a for a in pool["Properties"]["Schema"] if a["Name"] == "email")


def test_old_pool_logical_ids_unchanged(resources):
    for lid, typ in OLD_IDS.items():
        assert lid in resources, lid
        assert resources[lid]["Type"] == typ, lid


def test_old_pool_email_stays_immutable_and_unprotected(resources):
    _, pool = _pool(resources, "OncaUserPool")
    assert _email(pool) == {"Name": "email", "Required": True, "Mutable": False}
    assert "DeletionProtection" not in pool["Properties"]   # no new property on the live pool
    assert pool["DeletionPolicy"] == "Retain"


def test_v2_pool_email_mutable_and_protected(resources):
    lid, pool = _pool(resources, "OncaUserPoolV2")
    assert lid.startswith("OncaUserPoolV2")
    assert _email(pool) == {"Name": "email", "Required": True, "Mutable": True}
    assert pool["Properties"]["DeletionProtection"] == "ACTIVE"
    assert pool["DeletionPolicy"] == "Retain" and pool["UpdateReplacePolicy"] == "Retain"
    custom = {a["Name"]: a.get("Mutable") for a in pool["Properties"]["Schema"] if a["Name"] != "email"}
    assert custom == {"tenant": False, "tier": True}


def test_v2_mirrors_every_old_resource(resources):
    """Same clients, IdPs, groups and users on V2 (ids + "V2"), with the same properties apart
    from the pool reference."""
    by_type = lambda t, v2: sorted(k for k, r in resources.items()  # noqa: E731
                                   if r["Type"] == t and ("V2" in k) == v2)
    for t in set(OLD_IDS.values()) | {"AWS::Cognito::UserPool"}:
        assert len(by_type(t, True)) == len(by_type(t, False)), t
    old_pool, _ = _pool(resources, "OncaUserPool")
    new_pool, _ = _pool(resources, "OncaUserPoolV2")
    for lid, typ in OLD_IDS.items():
        if typ == "AWS::Cognito::UserPool":
            continue   # differs by design (email mutability, deletion protection)
        if typ == "AWS::Cognito::UserPoolClient":   # OncaUserPool<Client> -> OncaUserPoolV2<Client>V2
            stem = "OncaUserPoolV2" + lid[len("OncaUserPool"):-8] + "V2"
            twin = [k for k in resources if k[:-8] == stem]
        elif typ == "AWS::Cognito::UserPoolIdentityProvider":
            twin = [k for k in resources if k[:-8] == lid[:-8] + "V2"]
        else:
            twin = [lid + "V2"]
        assert len(twin) == 1 and twin[0] in resources, lid
        a = json.dumps(resources[lid]["Properties"], sort_keys=True).replace(old_pool, "POOL")
        b = json.dumps(resources[twin[0]]["Properties"], sort_keys=True).replace(new_pool, "POOL")
        assert a == b, lid


def test_cutover_v2_is_the_live_pool():
    """#262 P3 (cutover step C): V2 owns both Hosted-UI domains and every consumer; the original
    pool keeps only its pre-token trigger until P5."""
    assert 'build_identity_pool(self, "", distribution_domain=' in APP_SRC
    assert 'build_identity_pool(self, "V2", distribution_domain=' in APP_SRC
    assert "email_mutable=True, deletion_protection=True" in APP_SRC
    assert "_ident = _ident_v2\n" in APP_SRC
    assert "user_pool, user_pool_client = _ident.pool, _ident.client" in APP_SRC
    # the domains hang off `user_pool` (= V2) and nothing else adds a domain
    assert APP_SRC.count(".add_domain(") == 2
    assert 'user_pool_domain = user_pool.add_domain(\n            "OncaHostedUi",' in APP_SRC
    assert 'auth_custom_domain = user_pool.add_domain(\n                "OncaAuthCustomDomain",' in APP_SRC
    # the original pool is referenced only to build it, output its id and attach the trigger
    import re
    legacy_uses = re.findall(r"_ident_legacy\.[a-z_.]+", APP_SRC)
    assert sorted(set(legacy_uses)) == ["_ident_legacy.pool.add_trigger", "_ident_legacy.pool.user_pool_id"]
    assert "_ident_v2.pool.add_trigger(cognito.UserPoolOperation.PRE_TOKEN_GENERATION, pretoken_fn)" in APP_SRC
    for out in ("OncaUserPoolV2Id", "OncaWarroomClientV2Id", "OncaOAuthFacadeClientV2Id", "LegacyUserPoolId",
                "AuthCustomDomainTarget", "UserPoolId", "UserPoolClientId"):
        assert f'CfnOutput(self, "{out}"' in APP_SRC
    # consumers: all through the V2-bound names
    assert 'f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool.user_pool_id}"' in APP_SRC
    assert "jwt_audience=[user_pool_client.user_pool_client_id]" in APP_SRC
    assert '"ONCA_COGNITO_CLIENT_ID": user_pool_client.user_pool_client_id' in APP_SRC
    assert '"ONCA_OAUTH_COGNITO_CLIENT_ID": oauth_cognito_client.user_pool_client_id' in APP_SRC
    assert "oauth_cognito_client = _ident.oauth_client" in APP_SRC
    assert APP_SRC.count('"ONCA_USER_POOL_ID", user_pool.user_pool_id') == 2          # act API + session broker (#277)
    assert APP_SRC.count('"ONCA_USER_POOL_ID": user_pool.user_pool_id') == 1          # OAuth facade
    assert APP_SRC.count("resources=[user_pool.user_pool_arn]") == 2                   # facade + act IAM


def test_site_uses_the_v2_warroom_client():
    site = ROOT / "src" / "dashboard" / "site"
    for f in ("index.html", "v2/ask.js", "v2/context.js"):
        src = (site / f).read_text(encoding="utf-8")
        assert "7qlquhh56o06tp9bo8gp77p385" not in src, f       # the original pool's warroom client
        assert 'clientId: "' + V2_WARROOM_CLIENT_ID + '"' in src, f


def test_cdk_declared_users_match_the_template(resources):
    declared = identity.cdk_declared_users()
    in_tpl = {r["Properties"]["Username"] for k, r in resources.items()
              if r["Type"] == "AWS::Cognito::UserPoolUser" and "V2" not in k}
    assert in_tpl == set(declared)
    assert declared["qa-test-admin@onca.example"]["groups"] == ["operator"]
