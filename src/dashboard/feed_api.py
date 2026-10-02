"""Per-tenant scoped feed (`GET /api/feed`, issue #48 / ADR 016 SaaS tier).

Server-authoritative: the client never receives the full feed. A verified Cognito
identity → tenant → licensed ``modules`` (onca-tenant-config) → the feed is scoped to
those modules (plus any in-scope conglomerate's group lines, ADR 017) before it leaves
the server. This is the multi-tenant mechanism: one endpoint behind the JWT authorizer,
scoped per identity through the shared CloudFront — no per-tenant distributions.

The same projection backs both tier-1 planes: SaaS serves it from this shared endpoint;
an AWS Marketplace tenant runs the identical Lambda in its own account. Fail closed —
no identity, or a provisioned-but-empty tenant, gets nothing.
"""
from __future__ import annotations

import json
import os
from typing import Any


def _resp(status: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body, ensure_ascii=False, default=str),
    }


def upgrade_info() -> dict[str, Any]:
    """#187: may the dashboard send buyers to the storefront yet? OFF until Signals-Storefront
    #6/#7/#8 ship: before that a checkout can't attach to the tenant, and the customer would pay
    and get nothing. Flipping it is one env var (ONCA_UPGRADE_LIVE) on this Lambda."""
    from src.dashboard.tenant_config import combos_payload

    live = os.environ.get("ONCA_UPGRADE_LIVE", "false").lower() in ("1", "true", "yes")
    return {"live": live,
            "storefront": os.environ.get("ONCA_STOREFRONT_URL", "https://signals-llc.store"),
            # curated combos (ADR 024 amendment 2026-10-02) — the page reads them from here
            "combos": combos_payload()}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    from src.dashboard.auth import identity_from_event, industry_groups

    identity = identity_from_event(event)
    # This endpoint is tenant-only (no legacy operator fallback): a verified identity
    # is required, and it must be entitled to at least one module — either a
    # provisioned tenant (below), or a plain industry Cognito group (no tenant_config
    # row needed; see auth.industry_groups).
    if identity is None:
        return _resp(403, {"error": "forbidden"})

    from src.dashboard.tenant_config import get_tenant_config

    tenant_id = identity.tenant
    tier = None
    modules: list[str] = []
    billing = None
    if tenant_id:
        cfg = get_tenant_config(tenant_id)
        modules = list((cfg or {}).get("modules") or [])
        tier = (cfg or {}).get("tier")
        billing = (cfg or {}).get("billing")  # #187: trial / paid / lapsed (storefront tenants)
    if not modules:
        groups = industry_groups(identity)
        if groups:
            modules, tenant_id, tier = groups, None, "group"
    if not modules:  # fail closed — unprovisioned / no entitlement ⇒ nothing
        return _resp(403, {"error": "no entitlement", "tenant": identity.tenant, "billing": billing,
                           "upgrade": upgrade_info()})

    bucket = os.environ.get("ONCA_SITE_BUCKET")
    if not bucket:
        return _resp(500, {"error": "not configured"})
    try:
        import boto3

        from src.dashboard.feed_builder import scope_feed_for_entry_tenant, scope_feed_to_modules

        raw = boto3.client("s3").get_object(Bucket=bucket, Key="feed.json")["Body"].read()
        # ADR 016/024: an Entry tenant (paid Entry or its trial — effective tier "entry"; an
        # active SaaS sub lifts it to "saas" in tenant_config.effective_entitlement) gets the
        # Entry slice at Entry depth, never the full SaaS projection of its module.
        project = scope_feed_for_entry_tenant if tier == "entry" else scope_feed_to_modules
        scoped = project(json.loads(raw), modules)
        scoped["tenant"] = tenant_id
        scoped["tier"] = tier
        scoped["billing"] = billing
        scoped["upgrade"] = upgrade_info()
        return _resp(200, scoped)
    except Exception as exc:  # pragma: no cover - read-only, best-effort
        print(f"feed_api error: {exc}")
        return _resp(500, {"error": "feed unavailable"})
