"""Agentic API keys — machine credentials for a tenant's own agents/automation.

ADR: storefront/docs/adr-agentic-api-billing.md (pilot: Onça). A distinct
credential type from the human Cognito JWT — hashed at rest, shown to the
admin exactly once at creation, resolved to a tenant the same fail-closed way
every other surface resolves a tenant (from the stored row, never from the
request).

Table `onca-api-keys`: PK `key_hash` (sha256 of the raw secret — the raw
secret itself is never stored). GSI `tenant-index`: PK `tenant_id`, SK
`key_id` — lets the admin panel list/revoke a tenant's own keys without a
table scan.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import secrets
from typing import Any

VALID_SCOPES = ("ask",)


def _table(table: Any | None = None) -> Any:
    if table is not None:
        return table
    import boto3

    return boto3.resource("dynamodb").Table(
        os.environ.get("ONCA_API_KEYS_TABLE", "onca-api-keys")
    )


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _hash(raw_secret: str) -> str:
    return hashlib.sha256(raw_secret.encode()).hexdigest()


def generate_key(
    tenant_id: str, label: str, scopes: list[str], created_by: str,
    *, table: Any | None = None,
) -> dict[str, Any]:
    """Create a new key, returning the RAW secret — the only time it is ever
    visible. Rejects unknown scopes rather than silently granting nothing."""
    scopes = sorted({str(s).strip().lower() for s in (scopes or []) if str(s).strip()})
    bad = [s for s in scopes if s not in VALID_SCOPES]
    if bad:
        raise ValueError(f"unknown scope(s) {bad}; valid: {VALID_SCOPES}")
    if not scopes:
        raise ValueError("at least one scope is required")
    key_id = secrets.token_hex(6)
    raw_secret = f"sk_onca_{secrets.token_hex(24)}"
    item = {
        "key_hash": _hash(raw_secret),
        "key_id": key_id,
        "tenant_id": str(tenant_id),
        "prefix": raw_secret[:16],
        "label": str(label or "").strip()[:80] or "unlabeled",
        "scopes": scopes,
        "status": "active",
        "created_at": _now(),
        "created_by": str(created_by or "unknown"),
        "last_used_at": None,
    }
    _table(table).put_item(Item=item)
    return {**item, "secret": raw_secret}


def lookup_key(raw_secret: str, *, table: Any | None = None) -> dict[str, Any] | None:
    """Resolve an active key by its raw secret. Fail closed: missing, revoked,
    or a lookup error all return None (never partially-trust a row)."""
    if not raw_secret or not raw_secret.startswith("sk_onca_"):
        return None
    try:
        item = _table(table).get_item(Key={"key_hash": _hash(raw_secret)}).get("Item")
    except Exception:  # pragma: no cover - fail closed on a lookup error
        return None
    if not item or item.get("status") != "active":
        return None
    return item


def touch_last_used(key_hash: str, *, table: Any | None = None) -> None:
    """Best-effort — never let a metering side-effect break the response."""
    try:
        _table(table).update_item(
            Key={"key_hash": key_hash},
            UpdateExpression="SET last_used_at = :t",
            ExpressionAttributeValues={":t": _now()},
        )
    except Exception as exc:  # pragma: no cover
        print(f"Warning: touch_last_used failed: {exc}")


def list_keys(tenant_id: str, *, table: Any | None = None) -> list[dict[str, Any]]:
    """Redacted rows for the admin panel — never the hash, never the secret."""
    try:
        resp = _table(table).query(
            IndexName="tenant-index",
            KeyConditionExpression="tenant_id = :t",
            ExpressionAttributeValues={":t": str(tenant_id)},
        )
    except Exception as exc:  # pragma: no cover
        print(f"Warning: list_keys failed: {exc}")
        return []
    out = []
    for item in resp.get("Items") or []:
        out.append({k: v for k, v in item.items() if k != "key_hash"})
    return sorted(out, key=lambda r: r.get("created_at") or "", reverse=True)


def revoke_key(tenant_id: str, key_id: str, *, table: Any | None = None) -> bool:
    """Revoke a key — scoped to `tenant_id` so a tenant can only ever revoke
    its OWN keys, even if it somehow guessed another tenant's key_id."""
    t = _table(table)
    try:
        resp = t.query(
            IndexName="tenant-index",
            KeyConditionExpression="tenant_id = :t AND key_id = :k",
            ExpressionAttributeValues={":t": str(tenant_id), ":k": str(key_id)},
        )
    except Exception as exc:  # pragma: no cover
        print(f"Warning: revoke_key lookup failed: {exc}")
        return False
    items = resp.get("Items") or []
    if not items:
        return False
    key_hash = items[0]["key_hash"]
    try:
        t.update_item(
            Key={"key_hash": key_hash},
            UpdateExpression="SET #s = :r",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":r": "revoked"},
        )
    except Exception as exc:  # pragma: no cover
        print(f"Warning: revoke_key update failed: {exc}")
        return False
    return True
