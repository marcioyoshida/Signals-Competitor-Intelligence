"""POST /api/register — lazy Entry-tier self-registration for a first-time Google
login (see docs/google-oauth-runbook.md). A Google login that resolves to no
existing `tenant_config.map_federated_email` mapping gets no `custom:tenant` claim
(lambda_pretoken.py's fail-closed-by-omission) and hits the dashboard's honest
"no access" gate; the dashboard offers this endpoint as the one deliberate,
user-initiated escape from that gate — pick entry-tier industries, get an entry
tenant scoped to exactly those, nothing more. Requires an authenticated identity
(the same JWT authorizer as /api/feed) with NO existing tenant — an already-
provisioned caller is rejected rather than silently re-provisioned.
"""
from __future__ import annotations

import json
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
    if identity is None:
        return _resp(401, {"error": "unauthorized"})
    if identity.tenant:
        return _resp(409, {"error": "already provisioned", "tenant": identity.tenant})
    if not identity.email:
        return _resp(400, {"error": "no email on this identity — cannot self-register"})

    from src.dashboard.tenant_config import ENTRY_INDUSTRIES, self_register_entry_tenant

    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        body = {}
    industries = body.get("industries") if isinstance(body, dict) else None
    try:
        cfg = self_register_entry_tenant(identity.email, industries or [])
    except ValueError as exc:
        return _resp(400, {"error": str(exc), "choices": list(ENTRY_INDUSTRIES)})
    return _resp(200, cfg)
