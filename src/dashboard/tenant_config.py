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
    return {
        "tenant_id": str(tenant_id),
        "tier": str(item.get("tier") or "saas"),
        "modules": [str(m).strip().lower() for m in (item.get("modules") or []) if str(m).strip()],
    }


def put_tenant_config(
    tenant_id: str, tier: str, modules: list[str], *, table: Any | None = None
) -> dict[str, Any]:
    """Provision/update a tenant's entitlement. Idempotent upsert."""
    tier = str(tier)
    if tier not in VALID_TIERS:
        raise ValueError(f"tier must be one of {VALID_TIERS}, got {tier!r}")
    mods = sorted({str(m).strip().lower() for m in (modules or []) if str(m).strip()})
    _table(table).put_item(Item={"tenant_id": str(tenant_id), "tier": tier, "modules": mods})
    return {"tenant_id": str(tenant_id), "tier": tier, "modules": mods}


def entitled(config: dict[str, Any] | None, industries: Any) -> bool:
    """True iff any of `industries` is in the tenant's modules. Empty modules ⇒ False."""
    mods = set((config or {}).get("modules") or [])
    if not mods:
        return False
    return bool(mods & {str(i).strip().lower() for i in (industries or [])})
