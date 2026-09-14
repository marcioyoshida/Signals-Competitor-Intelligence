"""OncaApiKeysAdmin — a tenant's own Agentic API key management (ADR: storefront/
docs/adr-agentic-api-billing.md; pilot notes in this repo's
docs/2026-09-13-adr025-agentic-api-billing.md).

Self-service, JWT-gated: any authenticated tenant user manages ONLY their own
tenant's keys — `tenant_id` is resolved from the verified Cognito identity
(`custom:tenant`), never from the request body or a query param, matching the
fail-closed pattern every other tenant-scoped surface in this repo uses. This
is deliberately NOT gated behind the elevated-operator check
(`registry_api._authorize`) — that gate is for Onça's own internal control
plane over EVERY tenant; this is a tenant managing itself.

Routes (same Cognito-JWT HTTP API as `/api/ask`):
  GET  /api/keys          -> {"keys": [...], "usage": {...}}
  POST /api/keys          -> {"label": str, "scopes": [str]} -> the new key (secret shown ONCE)
  POST /api/keys/revoke   -> {"key_id": str} -> {"revoked": bool}
"""
from __future__ import annotations

from typing import Any

from src.dashboard.agent_ask import _body, _resp


def _method(event: dict[str, Any]) -> str:
    rc = (event.get("requestContext") or {}).get("http") or {}
    return str(rc.get("method") or event.get("httpMethod") or "GET").upper()


def _path(event: dict[str, Any]) -> str:
    return str(event.get("rawPath") or event.get("path") or "")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    from src.dashboard.auth import identity_from_event

    identity = identity_from_event(event)
    if identity is None or not identity.tenant:
        # No legacy/origin-secret fallback here on purpose: key management has no
        # meaning for the unscoped "legacy operator" mode other surfaces allow —
        # a caller with no verified tenant has no tenant's keys to manage.
        return _resp(403, {"error": "forbidden — a verified tenant identity is required"})
    tenant_id = identity.tenant

    from src.dashboard import api_keys, api_usage

    method = _method(event)
    path = _path(event)

    if method == "GET":
        keys = api_keys.list_keys(tenant_id)
        usage = api_usage.get_usage(tenant_id)
        return _resp(200, {"keys": keys, "usage": usage})

    if method == "POST" and path.endswith("/revoke"):
        body = _body(event)
        if body is None:
            return _resp(400, {"error": "invalid JSON body"})
        key_id = str(body.get("key_id") or "").strip()
        if not key_id:
            return _resp(400, {"error": "key_id required"})
        ok = api_keys.revoke_key(tenant_id, key_id)
        return _resp(200, {"revoked": ok})

    if method == "POST":
        body = _body(event)
        if body is None:
            return _resp(400, {"error": "invalid JSON body"})
        label = str(body.get("label") or "").strip()
        scopes = body.get("scopes") if isinstance(body.get("scopes"), list) else ["ask"]
        try:
            created = api_keys.generate_key(
                tenant_id, label, scopes, created_by=(identity.email or identity.sub),
            )
        except ValueError as exc:
            return _resp(400, {"error": str(exc)})
        # The secret is returned exactly once, here, and never again — the row
        # stored server-side only ever holds its hash.
        return _resp(200, {
            "key_id": created["key_id"], "secret": created["secret"],
            "prefix": created["prefix"], "label": created["label"],
            "scopes": created["scopes"], "created_at": created["created_at"],
        })

    return _resp(405, {"error": "method not allowed"})
