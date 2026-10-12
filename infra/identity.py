"""The Cognito identity block of OncaPrototypeStack, as a plain helper (#262).

`build_identity_pool(stack, suffix, ...)` creates one user pool with everything that hangs off it:
the Google/Apple IdPs, the warroom (Hosted-UI/PKCE) client, the OAuth-facade client, the industry
groups + the operator group, and the CDK-declared demo/fleet/QA users. It is a FUNCTION, not a
nested Construct, on purpose: every construct is created directly in the stack's scope, so the
existing pool (suffix "") keeps every logical id byte-identical (OncaUserPool4190A2E4,
OncaGroupBanking, OncaQaUserAdminOperatorGroup, ...) and the V2 pool gets the same ids + "V2".

#262: the original pool has `email` required + IMMUTABLE, which breaks the second federated sign-in
of every Google/Apple user (`user.email: Attribute cannot be updated`). Cognito can't change that
in place, so V2 (email required + MUTABLE) is built side by side and the consumers move over in a
later phase (docs/2026-10-12-cognito-pool-v2-migration.md). Domains are NOT created here: domain
names are global, so they stay on whichever pool currently owns them (app.py).

The user/group lists below are also read by scripts/migrate_cognito_pool.py (to tell CDK-declared
users from hand-made ones), so they live here as plain data, importable without synthesizing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Industry Cognito Groups — the lightweight, no-tenant-config-row entitlement
# path (auth.industry_groups / tenant_config.cognito_grant_industry_group):
# add a user to the "banking" group and they see exactly banking's dashboard,
# no DynamoDB row needed. One group per canonical industry slug (kept as a
# literal tuple, not imported from src.synth.entity_registry.INDUSTRIES,
# since infra/app.py's synth-time execution isn't on the same sys.path as
# the application source — same duplication convention feed_builder.py
# already uses for ENTRY_INDUSTRIES). Coarse for now — one group per
# industry, not yet per officer-within-industry (e.g. a future
# "banking-cso" group); that finer split is a later increment.
INDUSTRY_GROUPS = (
    "banking", "investment-banking", "insurance", "asset-management",
    "wealth-management", "private-markets", "fintech",
    "financial-data-analytics", "advisory", "crypto", "consorcio",
    "betting", "real-estate-funds", "agri-funds", "acquiring",
    "closed-pension", "securitization",
)
OPERATOR_GROUP = "operator"

# ADR 016 SaaS-plane demo tenants: (tenant_id, module slug). app.py seeds their
# onca-tenant-config rows; this module creates one Cognito user per tenant.
DEMO_TENANTS = (
    ("acquiring", "acquiring"),
    ("fintech", "fintech"),
    ("insurance", "insurance"),
    ("wealth-management", "wealth-management"),
)
FLEET_HEALTH_USER = ("fleet-health-check@onca.example", "acquiring", "saas")
# ADR 027 (#126) QA personas: (username, tenant, tier)
QA_USERS = (
    ("qa-test-entry@onca.example", "qa-internal-test", "entry"),
    ("qa-test-admin@onca.example", "qa-internal-admin", "sovereign"),
)
QA_OPERATOR_USER = "qa-test-admin@onca.example"


def cdk_declared_users() -> dict[str, dict[str, Any]]:
    """username -> {tenant, tier, groups} for every user this module declares in CloudFormation."""
    users = {f"demo-{tid}@onca.example": {"tenant": tid, "tier": "saas", "groups": []}
             for tid, _slug in DEMO_TENANTS}
    users[FLEET_HEALTH_USER[0]] = {"tenant": FLEET_HEALTH_USER[1], "tier": FLEET_HEALTH_USER[2], "groups": []}
    for username, tenant, tier in QA_USERS:
        users[username] = {"tenant": tenant, "tier": tier,
                           "groups": [OPERATOR_GROUP] if username == QA_OPERATOR_USER else []}
    return users


@dataclass
class IdentityPool:
    pool: Any
    client: Any            # the warroom / officer-suite Hosted-UI PKCE client
    oauth_client: Any      # the OAuth facade's static upstream client (#181)
    google_idp: Any = None
    apple_idp: Any = None
    federated: list = field(default_factory=list)
    operator_group: Any = None


def build_identity_pool(stack, suffix: str, *, distribution_domain: str,
                        email_mutable: bool = False, deletion_protection: bool = False) -> IdentityPool:
    """One user pool + IdPs + clients + groups + CDK users, every construct id ending in `suffix`.

    suffix "" MUST stay byte-identical to the pre-#262 stack (no new properties on the old pool:
    `deletion_protection` is only passed when True)."""
    from aws_cdk import Duration, RemovalPolicy, SecretValue
    from aws_cdk import aws_cognito as cognito

    self = stack  # the context lookups below read like the rest of app.py
    s = suffix
    pool_kwargs: dict[str, Any] = dict(
        self_sign_up_enabled=False,  # tenants are provisioned, not open self-signup
        sign_in_aliases=cognito.SignInAliases(email=True),
        standard_attributes=cognito.StandardAttributes(
            # #262: immutable on the original pool (the bug); mutable on V2.
            email=cognito.StandardAttribute(required=True, mutable=email_mutable),
        ),
        custom_attributes={
            # the tenant this user belongs to (entitlement key) + its tier.
            "tenant": cognito.StringAttribute(mutable=False),
            "tier": cognito.StringAttribute(mutable=True),  # entry | saas | sovereign
        },
        password_policy=cognito.PasswordPolicy(min_length=12),
        account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
        removal_policy=RemovalPolicy.RETAIN,  # never destroy identities on stack update
    )
    if deletion_protection:
        pool_kwargs["deletion_protection"] = True
    user_pool = cognito.UserPool(stack, f"OncaUserPool{s}", **pool_kwargs)

    # Google sign-in (ported from Signals-Creator-Radar/Bluefin's ADR 0017 pattern — see
    # docs/google-oauth-runbook.md). Gated on `google_client_id` context (persisted in
    # infra/cdk.json, not sensitive) — the callback URLs below are literal strings
    # (onssa.org / the distribution's own domain), never a construct reference, so adding a
    # federated provider here does not risk the CFN circular dependency Bluefin's runbook
    # warns about. Unlike Bluefin, this never auto-provisions a tenant — see
    # lambda_pretoken.py and tenant_config.map_federated_email.
    _google_client_id = self.node.try_get_context("google_client_id") or ""
    google_idp = None
    if _google_client_id:
        _google_secret = SecretValue.secrets_manager(
            "signalscompetitor/onca/google-oauth", json_field="client_secret"
        )
        google_idp = cognito.UserPoolIdentityProviderGoogle(
            stack, f"OncaGoogleIdp{s}",
            user_pool=user_pool,
            client_id=_google_client_id,
            client_secret_value=_google_secret,
            scopes=["openid", "email", "profile"],
            attribute_mapping=cognito.AttributeMapping(
                email=cognito.ProviderAttribute.GOOGLE_EMAIL,
                # Without this, Cognito defaults email_verified to false for every
                # federated user regardless of what Google actually asserts — the exact
                # gotcha Bluefin's runbook documents hitting live.
                email_verified=cognito.ProviderAttribute.GOOGLE_EMAIL_VERIFIED,
            ),
        )

    # #248 (ADR 029): Sign in with Apple, same pattern as Google — gated on context persisted in
    # infra/cdk.json (Services ID, team ID and key ID are not secrets; NEVER pass them as -c
    # flags only: an unrelated deploy without the flag would silently destroy the IdP, the
    # 2026-09-15 Google incident). The .p8 key lives only in Secrets Manager. Its own Onça App ID
    # / Services ID / key under team AWSP6DBQ68 — not grouped with Bluefin's (shared user ids
    # and consent branding). Federated users resolve by EMAIL exactly like Google
    # (lambda_pretoken / map_federated_email); a Hide-My-Email relay address won't match an
    # operator mapping — the no-access gate says so (#249 adds invite codes).
    _apple = {k: self.node.try_get_context(f"apple_{k}") or "" for k in ("services_id", "team_id", "key_id")}
    apple_idp = None
    if all(_apple.values()):
        apple_idp = cognito.UserPoolIdentityProviderApple(
            stack, f"OncaAppleIdp{s}",
            user_pool=user_pool,
            client_id=_apple["services_id"],
            team_id=_apple["team_id"],
            key_id=_apple["key_id"],
            private_key_value=SecretValue.secrets_manager(
                "signalscompetitor/onca/apple-oauth", json_field="private_key"),
            scopes=["email", "name"],
            attribute_mapping=cognito.AttributeMapping(
                email=cognito.ProviderAttribute.APPLE_EMAIL,
                email_verified=cognito.ProviderAttribute.APPLE_EMAIL_VERIFIED,
            ),
        )
    _federated = [p for p, idp in ((cognito.UserPoolClientIdentityProvider.GOOGLE, google_idp),
                                   (cognito.UserPoolClientIdentityProvider.APPLE, apple_idp)) if idp]

    _client_kwargs = dict(
        # user_srp: the browser/dashboard login flow (PKCE Hosted UI + SRP).
        # admin_user_password: ADDITIVE — enables AdminInitiateAuth with
        # ADMIN_USER_PASSWORD_AUTH so a trusted server-side caller (e.g. the
        # fleet-monitor health-check Lambda in a different AWS account/repo) can
        # do a full username+password login round-trip without implementing SRP.
        # Deliberately NOT user_password=True (the non-admin, broader flow) —
        # AdminInitiateAuth already requires IAM credentials with
        # cognito-idp:AdminInitiateAuth on this pool, so this stays admin-scoped.
        auth_flows=cognito.AuthFlow(user_srp=True, admin_user_password=True),
        o_auth=cognito.OAuthSettings(
            flows=cognito.OAuthFlows(authorization_code_grant=True),
            scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
            # "/" for the (deprecating) v1 warroom; "/exec" for the officer suite's
            # own PKCE login so it redirects back to /exec, not root.
            callback_urls=[
                f"https://{distribution_domain}/",
                f"https://{distribution_domain}/exec",
                # onssa.org is an alternate domain name on the SAME distribution
                # (added 2026-09-13) — Cognito validates redirect_uri literally,
                # so the branded domain needs its own entries or Hosted UI login
                # started from onssa.org/exec fails on the callback.
                "https://onssa.org/",
                "https://onssa.org/exec",
                "https://www.onssa.org/",
                "https://www.onssa.org/exec",
                # ADR 029 (#254/#255): the store app signs in through the system browser and
                # comes back on its own scheme; /exec in the app's WebView does the exchange.
                "onca://auth",
            ],
            logout_urls=[
                f"https://{distribution_domain}/",
                f"https://{distribution_domain}/exec",
                "https://onssa.org/",
                "https://onssa.org/exec",
                "https://www.onssa.org/",
                "https://www.onssa.org/exec",
                "onca://auth",
            ],
        ),
        prevent_user_existence_errors=True,
        # #164 mobile session posture (docs/onca-celular-seguranca): 1h ID tokens; the
        # refresh token (held ONLY in the /api/session httpOnly cookie) is capped at 30
        # days absolute — the per-tenant idle limit is the cookie's sliding Max-Age.
        # Revocation lets logout / operator global sign-out kill a refresh token.
        id_token_validity=Duration.hours(1),
        access_token_validity=Duration.hours(1),
        refresh_token_validity=Duration.days(30),
        enable_token_revocation=True,
    )
    if _federated:
        _client_kwargs["supported_identity_providers"] = [
            cognito.UserPoolClientIdentityProvider.COGNITO, *_federated]
    user_pool_client = user_pool.add_client(f"OncaWarroomClient{s}", **_client_kwargs)

    # #181/#182/#186: a PUBLIC client (PKCE, no secret) whose only callback is the OAuth AS's own —
    # the static upstream client of the MCP spec's confused-deputy rule, hence the per-client
    # consent page (app.py wires the AS Lambda to it).
    oauth_cognito_client = user_pool.add_client(
        f"OncaOAuthFacadeClient{s}",
        generate_secret=False,
        auth_flows=cognito.AuthFlow(user_srp=True),
        o_auth=cognito.OAuthSettings(
            flows=cognito.OAuthFlows(authorization_code_grant=True),
            scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
            callback_urls=["https://onssa.org/oauth/callback"],
            logout_urls=["https://onssa.org/"],
        ),
        supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO, *_federated],
        id_token_validity=Duration.hours(1),
        access_token_validity=Duration.hours(1),
        refresh_token_validity=Duration.days(1),
        prevent_user_existence_errors=True,
    )
    for _idp in (google_idp, apple_idp):
        if _idp:
            # A client's supported IDP must exist before the client does.
            user_pool_client.node.add_dependency(_idp)
            oauth_cognito_client.node.add_dependency(_idp)

    for _ind in INDUSTRY_GROUPS:
        cognito.CfnUserPoolGroup(
            stack,
            f"OncaGroup{_ind.title().replace('-', '')}{s}",
            user_pool_id=user_pool.user_pool_id,
            group_name=_ind,
            description=f"Single-industry dashboard access: {_ind}",
        )

    # ADR 016 addendum Decision 2 (2026-09-23): the elevated-operator GROUP —
    # what `act_api.py`/`registry_api.py` actually gate registry-write/agent-
    # act access on now, instead of the `tier` claim they used to also accept.
    # Before it existed, the seeded QA admin persona's elevated access came
    # ENTIRELY from its `custom:tier=sovereign` claim, i.e. any tenant simply
    # PROVISIONED at the sovereign pricing tier got registry-mutation rights on
    # shared SaaS infra. The QA admin persona (below) gets a real role grant.
    operator_group = cognito.CfnUserPoolGroup(
        stack,
        f"OncaGroupOperator{s}",
        user_pool_id=user_pool.user_pool_id,
        group_name=OPERATOR_GROUP,
        description="Elevated control-plane access: registry mutation, /api/act writes.",
    )

    def _user(cid: str, username: str, tenant: str, tier: str):
        # SUPPRESS ⇒ no invite email to the placeholder .example address; email_verified skips
        # verification. Users land in FORCE_CHANGE_PASSWORD — an operator sets a permanent
        # password out-of-band (`aws cognito-idp admin-set-user-password ... --permanent`, or
        # scripts/migrate_cognito_pool.py for V2). This stack must NEVER contain a password.
        # NB sign_in_aliases=SignInAliases(email=True) with no `username=True` makes email the
        # USERNAME ATTRIBUTE, so Username must itself be a valid email string.
        return cognito.CfnUserPoolUser(
            stack, cid,
            user_pool_id=user_pool.user_pool_id,
            username=username,
            message_action="SUPPRESS",
            user_attributes=[
                cognito.CfnUserPoolUser.AttributeTypeProperty(name="email", value=username),
                cognito.CfnUserPoolUser.AttributeTypeProperty(name="email_verified", value="true"),
                cognito.CfnUserPoolUser.AttributeTypeProperty(name="custom:tenant", value=tenant),
                cognito.CfnUserPoolUser.AttributeTypeProperty(name="custom:tier", value=tier),
            ],
        )

    # One demo Cognito user per ADR 016 demo SaaS tenant (app.py seeds the tenant rows).
    for (tid, _slug) in DEMO_TENANTS:
        _uid = tid.replace("-", "").capitalize()
        _user(f"OncaDemoUser{_uid}{s}", f"demo-{tid}@onca.example", tid, "saas")

    # One dedicated, low-privilege synthetic-monitoring user for the (separate,
    # unrelated repo) fleet-monitor Lambda to exercise a full Cognito login
    # round-trip on a schedule — proof the auth stack works end-to-end, not just
    # that the pool exists. Entitled identically to the "acquiring" demo tenant
    # purely so a JWT-gated GET (e.g. /api/gaps) returns 200 instead of 403.
    _user(f"OncaFleetHealthCheckUser{s}", *FLEET_HEALTH_USER)

    # ADR 027 (#126): two dedicated QA personas for the automated Playwright
    # navigation/visual pipeline — never a real design partner's tenant. Their
    # `onca-tenant-config` rows are created separately via `provision_tenant.py`;
    # this only creates the Cognito identities.
    for _qa_username, _qa_tenant, _qa_tier in QA_USERS:
        _qa_user = _user(f"OncaQaUser{_qa_tier.capitalize()}{s}", _qa_username, _qa_tenant, _qa_tier)
        if _qa_username == QA_OPERATOR_USER:
            # ADR 016 addendum Decision 2: this persona's elevated access needs the
            # real "operator" role grant, or ADR 027's QA pipeline loses coverage of
            # the elevated/multi-industry surface.
            _qa_attachment = cognito.CfnUserPoolUserToGroupAttachment(
                stack, f"OncaQaUserAdminOperatorGroup{s}",
                user_pool_id=user_pool.user_pool_id,
                username=_qa_username,
                group_name=operator_group.group_name,
            )
            # `username` above is a plain string, not a CDK token, so CFN
            # wouldn't otherwise infer this must wait for the user to exist.
            _qa_attachment.add_dependency(_qa_user)

    return IdentityPool(pool=user_pool, client=user_pool_client, oauth_client=oauth_cognito_client,
                        google_idp=google_idp, apple_idp=apple_idp, federated=_federated,
                        operator_group=operator_group)
