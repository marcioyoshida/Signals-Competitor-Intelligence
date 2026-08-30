"""Phase C identity (Cognito) — ADR 002 Decision 7 ("7 gates 6").

Per-tenant identity is the prerequisite for entitlement and the distribution tiers
(ADR 015/016): the shared basic-auth edge cannot attribute a request to a tenant.
This module extracts the authenticated tenant identity from a request.

**Verification is upstream by design.** The Cognito JWT is verified before the
handler runs (the next increment wires *how*: Lambda URLs have no built-in
authorizer, so it's an API Gateway JWT authorizer, a Lambda@Edge verify, or an
in-Lambda JWKS verify). This module reads ONLY already-verified claims from the
request context — it never decodes or trusts a raw bearer token for entitlement.
Until the verifier is wired, `identity_from_event` returns None and callers stay in
the legacy operator (origin-secret) mode.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Identity:
    sub: str
    tenant: str | None = None
    tier: str | None = None  # entry | saas | sovereign
    email: str | None = None
    groups: list[str] = field(default_factory=list)


def _claims(event: dict[str, Any]) -> dict[str, Any] | None:
    """Verified claims from an authorizer context, if present.

    Supports both HTTP API (`requestContext.authorizer.jwt.claims`) and REST-style
    (`requestContext.authorizer.claims`) shapes. Returns None when no verified
    identity is attached (⇒ legacy operator/origin-secret mode)."""
    rc = (event or {}).get("requestContext") or {}
    authz = rc.get("authorizer") or {}
    if not isinstance(authz, dict):
        return None
    jwt = authz.get("jwt")
    if isinstance(jwt, dict) and isinstance(jwt.get("claims"), dict):
        return jwt["claims"]
    if isinstance(authz.get("claims"), dict):
        return authz["claims"]
    return None


def _groups(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(g) for g in raw if str(g).strip()]
    if isinstance(raw, str) and raw.strip():
        # Cognito can flatten cognito:groups to "[a b c]" or "a,b".
        inner = raw.strip().strip("[]")
        parts = [p for chunk in inner.split(",") for p in chunk.split()]
        return [p for p in parts if p]
    return []


def identity_from_event(event: dict[str, Any]) -> Identity | None:
    """Return the verified tenant Identity for a request, or None (no identity).

    Reads a tenant from `custom:tenant` (or `tenant`) and tier from `custom:tier`
    (or `tier`). None means no verified identity is attached — callers should fall
    back to the legacy operator gate, never fabricate a tenant."""
    claims = _claims(event)
    if not claims:
        return None
    sub = str(claims.get("sub") or "").strip()
    if not sub:
        return None
    return Identity(
        sub=sub,
        tenant=(claims.get("custom:tenant") or claims.get("tenant") or None),
        tier=(claims.get("custom:tier") or claims.get("tier") or None),
        email=(claims.get("email") or None),
        groups=_groups(claims.get("cognito:groups")),
    )
