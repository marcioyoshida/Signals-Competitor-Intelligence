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


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    from src.dashboard.auth import identity_from_event

    identity = identity_from_event(event)
    # This endpoint is tenant-only (no legacy operator fallback): a verified identity
    # with a tenant is required.
    if identity is None or not identity.tenant:
        return _resp(403, {"error": "forbidden"})

    from src.dashboard.tenant_config import get_tenant_config

    cfg = get_tenant_config(identity.tenant)
    modules = list((cfg or {}).get("modules") or [])
    if not modules:  # fail closed — unprovisioned / no entitlement ⇒ nothing
        return _resp(403, {"error": "no entitlement", "tenant": identity.tenant})

    bucket = os.environ.get("ONCA_SITE_BUCKET")
    if not bucket:
        return _resp(500, {"error": "not configured"})
    try:
        import boto3

        from src.dashboard.feed_builder import scope_feed_to_modules

        raw = boto3.client("s3").get_object(Bucket=bucket, Key="feed.json")["Body"].read()
        scoped = scope_feed_to_modules(json.loads(raw), modules)
        scoped["tenant"] = identity.tenant
        scoped["tier"] = cfg.get("tier")
        return _resp(200, scoped)
    except Exception as exc:  # pragma: no cover - read-only, best-effort
        print(f"feed_api error: {exc}")
        return _resp(500, {"error": "feed unavailable"})
