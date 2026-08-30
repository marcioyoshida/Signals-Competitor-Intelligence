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
    table: Any | None = None,
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
    _table(table).put_item(
        Item={"tenant_id": str(tenant_id), "tier": tier, "modules": mods, "plane": plane})
    return {"tenant_id": str(tenant_id), "tier": tier, "modules": mods, "plane": plane}


def entitled(config: dict[str, Any] | None, industries: Any) -> bool:
    """True iff any of `industries` is in the tenant's modules. Empty modules ⇒ False."""
    mods = set((config or {}).get("modules") or [])
    if not mods:
        return False
    return bool(mods & {str(i).strip().lower() for i in (industries or [])})
