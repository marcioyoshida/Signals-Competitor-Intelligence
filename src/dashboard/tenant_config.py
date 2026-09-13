"""Phase D — per-tenant entitlement (ADR 002 Phase D + ADR 016).

`onca-tenant-config`: tenant_id -> {tier, modules[]}. The single entitlement source
of truth: the read boundary scopes the feed/agent to `modules[]`; `tier` selects the
delivery plane. **Fail closed** — a tenant with no record (or empty modules) has NO
entitlement, so a verified-but-unprovisioned user sees nothing rather than everything.
"""
from __future__ import annotations

import os
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
    return {
        "tenant_id": str(tenant_id),
        "tier": tier,
        "modules": [str(m).strip().lower() for m in (item.get("modules") or []) if str(m).strip()],
        # ADR 016 delivery plane: portal (Entry static) | saas (shared multi-tenant) |
        # marketplace (the SAME product in the tenant's own AWS account). tier-1 is SaaS
        # OR Marketplace — same per-tenant read boundary either way.
        "plane": str(item.get("plane") or _default_plane(tier)),
    }


# The delivery plane a tier defaults to (overridable per tenant): a tier-1 tenant is
# `saas` unless explicitly provisioned as `marketplace` (in-account).
VALID_PLANES = ("portal", "saas", "marketplace")


def _default_plane(tier: str) -> str:
    return "portal" if tier == "entry" else "saas"


def put_tenant_config(
    tenant_id: str, tier: str, modules: list[str], *, plane: str | None = None,
    table: Any | None = None, force_not_ready: bool = False,
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
    _table(table).put_item(
        Item={"tenant_id": str(tenant_id), "tier": tier, "modules": mods, "plane": plane})
    return {"tenant_id": str(tenant_id), "tier": tier, "modules": mods, "plane": plane}


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


def entitled(config: dict[str, Any] | None, industries: Any) -> bool:
    """True iff any of `industries` is in the tenant's modules. Empty modules ⇒ False."""
    mods = set((config or {}).get("modules") or [])
    if not mods:
        return False
    return bool(mods & {str(i).strip().lower() for i in (industries or [])})
