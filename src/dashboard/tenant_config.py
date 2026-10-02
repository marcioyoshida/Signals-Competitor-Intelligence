"""Phase D — per-tenant entitlement (ADR 002 Phase D + ADR 016).

`onca-tenant-config`: tenant_id -> {tier, modules[]}. The single entitlement source
of truth: the read boundary scopes the feed/agent to `modules[]`; `tier` selects the
delivery plane. **Fail closed** — a tenant with no record (or empty modules) has NO
entitlement, so a verified-but-unprovisioned user sees nothing rather than everything.
"""
from __future__ import annotations

import os
import re
from typing import Any

VALID_TIERS = ("entry", "saas", "sovereign")

# ADR 016 — the Entry Portal carries ONLY the entry-tier verticals, at shallow
# public-filing depth. This is the single source of truth for that set (canonical
# registry slugs). An `entry` tenant may license a SUBSET of these and nothing else;
# the higher-tier industries are never fed to Entry (enforced here at provisioning
# AND at feed-build via feed_builder.derive_entry_feed — see "fork the feed").
ENTRY_INDUSTRIES = ("agri-funds", "betting", "consorcio", "crypto", "real-estate-funds")


def allowed_industries_for_tier(tier: str) -> frozenset[str] | None:
    """The industry allow-list a tier may license. Entry is capped to the entry-tier
    verticals; SaaS/Sovereign are unrestricted (None = any registered industry)."""
    return frozenset(ENTRY_INDUSTRIES) if tier == "entry" else None


# #119 (ADR 024 readiness pass, 2026-09-12): sectors thin across EVERY officer — not a display
# artifact, a genuine "don't sell this yet" signal (see docs/2026-09-11-adr-launch-readiness.md,
# "Coverage-gated GA sector list"). Recorded as excluded-until-revisited rather than left to
# surface by accident in a provisioning call. Revisit only with a named buyer + an ingestion plan.
NOT_READY_INDUSTRIES = ("closed-pension", "securitization", "private-markets")


# #187 (owner decisions 2026-09-28): Entry is PAID with a 14-day trial (self-registration
# starts the trial); SaaS is sold per module, one storefront price per ADR 024 band. A
# storefront-managed tenant's entitlement is DERIVED at read time from {trial window, paid
# module subscriptions, operator base}, so an expired trial or a lapsed subscription takes
# effect without a job. Operator-provisioned tenants (no `billing_managed`) are unchanged.
TRIAL_DAYS = 14
SAAS_BANDS = {  # ADR 024 SaaS price bands → the modules each band's price covers
    "saas_premium": ("banking", "investment-banking", "private-markets"),
    "saas_mid": ("insurance", "asset-management", "wealth-management",
                 "financial-data-analytics", "acquiring", "agri-funds"),
    "saas_entry": ("fintech", "advisory", "betting", "crypto", "consorcio", "real-estate-funds"),
}
STOREFRONT_TIERS = ("entry",) + tuple(SAAS_BANDS)


def module_allowed(storefront_tier: str, module: str) -> bool:
    """May a purchase at ``storefront_tier`` license ``module``? The buyer picks the module at
    checkout, so the band's price must actually cover it (never banking at the 2.900 price)."""
    module = str(module or "").strip().lower()
    if module in NOT_READY_INDUSTRIES:
        return False
    if storefront_tier == "entry":
        return module in ENTRY_INDUSTRIES
    return module in SAAS_BANDS.get(storefront_tier, ())


def _now() -> "Any":
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc)


def effective_entitlement(item: dict[str, Any], now: Any = None) -> dict[str, Any]:
    """Pure: the tier/modules a storefront-managed tenant holds at ``now``, plus a ``billing``
    summary for the UI. Base (operator) modules always count; a paid module counts while its
    subscription is active; trial modules count until ``trial_until``. Fails closed: a trial
    date that doesn't parse has ended."""
    import datetime as _dt

    now = now or _now()
    base = [str(m).strip().lower() for m in (item.get("modules") or []) if str(m).strip()]
    subs = item.get("subs") or {}
    active = {m: s for m, s in subs.items() if (s or {}).get("status") == "active"}
    trial_until = str(item.get("trial_until") or "")
    try:
        trial_on = bool(trial_until) and now < _dt.datetime.fromisoformat(trial_until)
    except ValueError:
        trial_on = False
    trial_mods = [str(m) for m in (item.get("trial_modules") or [])] if trial_on else []
    mods = sorted(set(base) | set(active) | set(trial_mods))
    base_tier = str(item.get("tier") or "entry")
    tier = ("saas" if any((s or {}).get("tier") == "saas" for s in active.values())
            and base_tier == "entry" else base_tier)
    if active:
        state = "active"
    elif trial_on:
        state = "trial"
    elif subs:
        state = "lapsed"
    elif trial_until:
        state = "trial_expired"
    else:
        state = "none"
    return {"tier": tier, "modules": mods,
            "billing": {"state": state, "trial_until": trial_until or None,
                        "trial_modules": [str(m) for m in (item.get("trial_modules") or [])],
                        "paid_modules": sorted(active), "base_modules": sorted(base),
                        "lapsed_modules": sorted(m for m, s in subs.items()
                                                 if (s or {}).get("status") != "active"),
                        "bands": {m: (s or {}).get("band") for m, s in subs.items()}}}


def _table(table: Any | None = None) -> Any:
    if table is not None:
        return table
    import boto3

    return boto3.resource("dynamodb").Table(
        os.environ.get("ONCA_TENANT_CONFIG_TABLE", "onca-tenant-config")
    )


def get_tenant_config(tenant_id: str | None, *, table: Any | None = None) -> dict[str, Any] | None:
    """Return {tenant_id, tier, modules} or None (unprovisioned ⇒ no entitlement)."""
    if not tenant_id:
        return None
    try:
        item = _table(table).get_item(Key={"tenant_id": str(tenant_id)}).get("Item")
    except Exception:  # pragma: no cover - fail closed on a lookup error
        return None
    if not item:
        return None
    tier = str(item.get("tier") or "saas")
    modules = [str(m).strip().lower() for m in (item.get("modules") or []) if str(m).strip()]
    billing = None
    if item.get("billing_managed"):  # #187: trial window + paid modules, derived now
        eff = effective_entitlement(item)
        tier, modules, billing = eff["tier"], eff["modules"], eff["billing"]
    return {
        "tenant_id": str(tenant_id),
        "tier": tier,
        "modules": modules,
        "billing": billing,
        # ADR 016 delivery plane: portal (Entry static) | saas (shared multi-tenant) |
        # marketplace (the SAME product in the tenant's own AWS account). tier-1 is SaaS
        # OR Marketplace — same per-tenant read boundary either way.
        "plane": str(item.get("plane") or _default_plane(str(item.get("tier") or "saas"))),
        # ADR 016 addendum Decision 3: the IAM role ARN this marketplace-plane
        # tenant's synth Lambda calls POST /resolve as — the trust-policy
        # artifact exchanged during onboarding (infra/tenant_stack.py's
        # `OncaResolveCallerRole`). None for every non-marketplace tenant.
        "resolve_caller_role_arn": item.get("resolve_caller_role_arn") or None,
    }


# The delivery plane a tier defaults to (overridable per tenant): a tier-1 tenant is
# `saas` unless explicitly provisioned as `marketplace` (in-account).
VALID_PLANES = ("portal", "saas", "marketplace")


def _default_plane(tier: str) -> str:
    return "portal" if tier == "entry" else "saas"


def put_tenant_config(
    tenant_id: str, tier: str, modules: list[str], *, plane: str | None = None,
    table: Any | None = None, force_not_ready: bool = False,
    resolve_caller_role_arn: str | None = None,
) -> dict[str, Any]:
    """Provision/update a tenant's entitlement. Idempotent upsert."""
    tier = str(tier)
    if tier not in VALID_TIERS:
        raise ValueError(f"tier must be one of {VALID_TIERS}, got {tier!r}")
    plane = str(plane) if plane else _default_plane(tier)
    if plane not in VALID_PLANES:
        raise ValueError(f"plane must be one of {VALID_PLANES}, got {plane!r}")
    mods = sorted({str(m).strip().lower() for m in (modules or []) if str(m).strip()})
    # Entry tenants are capped to the entry-tier verticals (ADR 016): a higher-tier
    # industry must never enter an Entry tenant's entitlement, so the Entry dashboard
    # can never surface it. Reject rather than silently drop — a mis-scoped provision
    # is an operator error worth surfacing.
    allowed = allowed_industries_for_tier(tier)
    if allowed is not None:
        bad = [m for m in mods if m not in allowed]
        if bad:
            raise ValueError(
                f"tier {tier!r} may only license entry-tier industries "
                f"{sorted(allowed)}; got disallowed {bad}"
            )
    # #119: not-ready sectors are excluded from EVERY tier by default, not just Entry — an
    # operator provisioning a SaaS design partner is exactly the scenario this guards against,
    # since SaaS/Sovereign have no allow-list otherwise. `force_not_ready=True` is the deliberate
    # escape hatch for a named buyer with an explicit ingestion plan (see NOT_READY_INDUSTRIES).
    if not force_not_ready:
        not_ready = [m for m in mods if m in NOT_READY_INDUSTRIES]
        if not_ready:
            raise ValueError(
                f"{sorted(not_ready)} are not launch-ready (issue #119) — pass "
                "force_not_ready=True to override for a named buyer with an ingestion plan"
            )
    if resolve_caller_role_arn and not re.match(
        r"^arn:aws:iam::\d{12}:role/.+$", str(resolve_caller_role_arn)
    ):
        raise ValueError(
            f"resolve_caller_role_arn must be an IAM role ARN "
            f"(arn:aws:iam::<account>:role/<name>), got {resolve_caller_role_arn!r}"
        )
    item: dict[str, Any] = {
        "tenant_id": str(tenant_id), "tier": tier, "modules": mods, "plane": plane,
    }
    # DynamoDB's Table resource rejects a bare `None` attribute value — omit
    # the key entirely rather than write a NULL, same convention the rest of
    # this module already follows for optional fields.
    if resolve_caller_role_arn:
        item["resolve_caller_role_arn"] = str(resolve_caller_role_arn)
    # #187: an operator re-put replaces the whole item. It must not wipe what the storefront
    # manages (paid subscriptions, the trial), or a paying customer silently loses access.
    try:
        prior = _table(table).get_item(Key={"tenant_id": str(tenant_id)}).get("Item") or {}
    except Exception:  # pragma: no cover - a read failure must not block provisioning
        prior = {}
    for k in _BILLING_FIELDS:
        if k in prior and k not in item:
            item[k] = prior[k]
    _table(table).put_item(Item=item)
    return dict(item)


_BILLING_FIELDS = ("billing_managed", "subs", "trial_until", "trial_modules")


def apply_purchase(tenant_id: str, storefront_tier: str, module: str, action: str, *,
                   event_id: str, table: Any | None = None) -> dict[str, Any]:
    """Grant or revoke ONE paid module subscription on an existing tenant (#187, called by the
    storefront dispatch via ``src.dashboard.upgrade``). ``action`` is "grant" or "revoke". A
    revoke marks the module lapsed rather than deleting it (history stays; re-buying restores).
    Raises KeyError for an unknown tenant, ValueError for a module the tier doesn't cover."""
    if action not in ("grant", "revoke"):
        raise ValueError("action must be grant or revoke")
    if storefront_tier not in STOREFRONT_TIERS or not module_allowed(storefront_tier, module):
        raise ValueError(f"{storefront_tier!r} does not cover module {module!r}")
    t = _table(table)
    item = t.get_item(Key={"tenant_id": str(tenant_id)}).get("Item")
    if not item or str(tenant_id).startswith("BILLING#"):
        raise KeyError("no such tenant")
    module = str(module).strip().lower()
    subs = dict(item.get("subs") or {})
    subs[module] = {"tier": "entry" if storefront_tier == "entry" else "saas",
                    "band": storefront_tier, "status": "active" if action == "grant" else "lapsed",
                    "event_id": str(event_id), "updated_at": _now().isoformat(timespec="seconds")}
    item["subs"] = subs
    item["billing_managed"] = True
    t.put_item(Item=item)
    return effective_entitlement(item)


def cognito_upsert_user(
    pool_id: str, email: str, tenant_id: str, tier: str, *, client: Any | None = None,
) -> str:
    """Create/update the Cognito user that carries `custom:tenant`/`custom:tier` for a real
    tenant — closing the gap where only 4 hardcoded demo tenants get a Cognito user
    (infra/app.py's deploy-time seed); a real design partner had no scripted path at all.

    `custom:tenant` is IMMUTABLE (infra/app.py's UserPool definition), so this refuses to
    silently relink an existing email to a different tenant — that would either fail at the
    API call or, worse, look like it worked while attaching the wrong identity. Returns
    "created" or "updated" (tier only, the one mutable field — e.g. an entry→saas upgrade)."""
    if client is None:
        import boto3

        client = boto3.client("cognito-idp")
    try:
        existing = client.admin_get_user(UserPoolId=pool_id, Username=email)
    except client.exceptions.UserNotFoundException:
        client.admin_create_user(
            UserPoolId=pool_id,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "custom:tenant", "Value": str(tenant_id)},
                {"Name": "custom:tier", "Value": str(tier)},
            ],
            DesiredDeliveryMediums=["EMAIL"],
        )
        return "created"
    cur_tenant = next(
        (a["Value"] for a in existing.get("UserAttributes", []) if a["Name"] == "custom:tenant"),
        None,
    )
    if cur_tenant and cur_tenant != str(tenant_id):
        raise ValueError(
            f"{email!r} is already linked to tenant {cur_tenant!r}, not {tenant_id!r} — "
            "custom:tenant is immutable, use a different email or a new user"
        )
    client.admin_update_user_attributes(
        UserPoolId=pool_id, Username=email,
        UserAttributes=[{"Name": "custom:tier", "Value": str(tier)}],
    )
    return "updated"


def _federated_table(table: Any | None = None) -> Any:
    if table is not None:
        return table
    import boto3

    return boto3.resource("dynamodb").Table(
        os.environ.get("ONCA_FEDERATED_MAP_TABLE", "onca-federated-tenant-map")
    )


def map_federated_email(
    email: str, tenant_id: str, tier: str, *, table: Any | None = None,
) -> dict[str, Any]:
    """Register the tenant a Google login for `email` should resolve to (Google OAuth,
    ported from Bluefin's ADR 0017 pattern — see docs/google-oauth-runbook.md).

    Onça never auto-provisions from a federated login the way Bluefin does: Cognito
    creates a Google-authenticated user internally with no `custom:tenant` attribute,
    and that attribute is `mutable=False` — confirmed live in the Bluefin fork,
    `AdminUpdateUserAttributes` raises even when the attribute was never set at all, so
    there is no API call that can ever write it after the fact. The tenant therefore
    lives here instead, keyed by email (known to the operator BEFORE the person's first
    Google login, unlike a Google `sub`), and is injected into every token via
    `claimsOverrideDetails` by `lambda_pretoken.py` — read identically to a real
    attribute by every downstream JWT-authorized API.

    An email with no row here is not an error at login time — the pre-token trigger
    just issues a token with no `custom:tenant` claim, and the existing per-tenant read
    boundary (feed_api.py) already 403s on that. This call is what PREVENTS that outcome
    for an invited design partner, run once per person when they're onboarded (alongside
    `cognito_upsert_user` for their password-login option, if any)."""
    if tier not in VALID_TIERS:
        raise ValueError(f"unknown tier {tier!r}, expected one of {VALID_TIERS}")
    email = email.strip().lower()
    if not email:
        raise ValueError("email is required")
    _federated_table(table).put_item(
        Item={"email": email, "tenant_id": str(tenant_id), "tier": tier}
    )
    return {"email": email, "tenant_id": str(tenant_id), "tier": tier}


def _allocate_entry_tenant_id(*, table: Any | None = None) -> str:
    """An OPAQUE id (#202): the tenant id is a key in usage/billing rows, the Stripe ``ref``,
    logs and URLs, so it must never carry any part of the user's email."""
    import secrets

    for _ in range(6):
        candidate = f"entry-{secrets.token_hex(6)}"
        if get_tenant_config(candidate, table=table) is None:
            return candidate
    raise RuntimeError("could not allocate a unique entry tenant id")  # pragma: no cover


def self_register_entry_tenant(
    email: str, industries: list[str], *, table: Any | None = None,
    federated_table: Any | None = None,
) -> dict[str, Any]:
    """Lazy self-registration for a first-time Google login: the ONE write path where an
    UNPROVISIONED identity creates its own tenant. #187 (owner, 2026-09-28): Entry is paid, so
    this starts a **14-day trial** of exactly **one** entry-tier sector (ADR 024: Entry is one
    vertical per tenant); after the trial, access needs an Entry subscription bought on the
    storefront (`apply_purchase`). It still cannot escalate past the entry tier: the pick is
    checked against `ENTRY_INDUSTRIES` server-side, and the tenant_id is freshly allocated,
    never caller-supplied, so this can't attach to (or overwrite) an existing tenant."""
    import datetime as _dt

    email = (email or "").strip().lower()
    if not email:
        raise ValueError("email is required")
    picked = sorted({
        str(i).strip().lower() for i in (industries or [])
        if str(i).strip().lower() in ENTRY_INDUSTRIES
    })
    if len(picked) != 1:
        raise ValueError(f"pick exactly one entry-tier sector: {sorted(ENTRY_INDUSTRIES)}")
    tenant_id = _allocate_entry_tenant_id(table=table)
    until = (_now() + _dt.timedelta(days=TRIAL_DAYS)).isoformat(timespec="seconds")
    item = {"tenant_id": tenant_id, "tier": "entry", "modules": [], "plane": "portal",
            "billing_managed": True, "trial_until": until, "trial_modules": picked}
    _table(table).put_item(Item=item)
    map_federated_email(email, tenant_id, "entry", table=federated_table)
    return {"tenant_id": tenant_id, "tier": "entry", "modules": picked, "plane": "portal",
            "billing": effective_entitlement(item)["billing"]}


def cognito_grant_industry_group(
    pool_id: str, email: str, industry: str, *, client: Any | None = None,
) -> str:
    """Add `email` to the Cognito group named `industry` — the lightweight, no-
    tenant-config-row entitlement path (see auth.industry_groups): a single Cognito
    group membership scopes that person to exactly that industry's dashboard/feed/
    ask, no `custom:tenant`/`custom:tier` attributes or DynamoDB row required.

    Creates the Cognito user first (sending Cognito's own invite email) if they don't
    already exist — WITHOUT `custom:tenant`/`custom:tier`, since group membership
    alone is the entitlement here. Validates `industry` against the canonical
    taxonomy so a typo can't silently create a group nothing ever matches (the group
    itself must already exist — see infra/app.py's per-industry CfnUserPoolGroup —
    admin_add_user_to_group raises on an unknown group name).
    Returns "created" (new user) or "granted" (existing user, group added)."""
    from src.synth.entity_registry import INDUSTRIES

    industry = industry.strip().lower()
    if industry not in INDUSTRIES:
        raise ValueError(f"{industry!r} is not a known industry slug: {sorted(INDUSTRIES)}")
    if client is None:
        import boto3

        client = boto3.client("cognito-idp")
    outcome = "granted"
    try:
        client.admin_get_user(UserPoolId=pool_id, Username=email)
    except client.exceptions.UserNotFoundException:
        client.admin_create_user(
            UserPoolId=pool_id,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            DesiredDeliveryMediums=["EMAIL"],
        )
        outcome = "created"
    client.admin_add_user_to_group(UserPoolId=pool_id, Username=email, GroupName=industry)
    return outcome


def entitled(config: dict[str, Any] | None, industries: Any) -> bool:
    """True iff any of `industries` is in the tenant's modules. Empty modules ⇒ False."""
    mods = set((config or {}).get("modules") or [])
    if not mods:
        return False
    return bool(mods & {str(i).strip().lower() for i in (industries or [])})
