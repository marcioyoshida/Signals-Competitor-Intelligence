"""Resource-server side of /mcp, /mcp/ops and /a2a: bearer token → principal, or a 401 challenge.

Checks, in order: the token comes from the Authorization header only (T7); signature, ES256,
our issuer, THIS resource's audience, exp/nbf (T11, T20); the resource's scope; the token's
refresh family still exists and isn't revoked (T14), cached ≤ 60 s. Entitlement (licensed
modules) is recomputed from the tenant record on every call by the caller — never trusted
from the token.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from src.dashboard.oauth import config, tokens

FAMILY_CACHE_S = 60
_family_cache: dict[tuple[str, str], tuple[float, bool]] = {}


def bearer(headers: dict[str, str]) -> str | None:
    raw = (headers or {}).get("authorization") or ""
    scheme, _, value = raw.partition(" ")
    return value.strip() or None if scheme.lower() == "bearer" else None


def challenge(settings: config.Settings, path: str, error: str | None = None) -> str:
    parts = ['Bearer resource_metadata="%s"' % settings.prm_url(path),
             'scope="%s"' % config.RESOURCES[path]]
    if error:
        parts.append('error="%s"' % error)
    return ", ".join(parts)


def family_active(sub: str, fid: str, lookup: Callable[[str, str], dict | None],
                  now: float | None = None) -> bool:
    now = time.time() if now is None else now
    hit = _family_cache.get((sub, fid))
    if hit and now - hit[0] < FAMILY_CACHE_S:
        return hit[1]
    fam = lookup("usr#" + sub, "fam#" + fid)
    ok = bool(fam) and not fam.get("revoked")
    _family_cache[(sub, fid)] = (now, ok)
    return ok


def principal(headers: dict[str, str], *, path: str, settings: config.Settings,
              public_keys: dict[str, bytes], family_lookup: Callable[[str, str], dict | None]
              ) -> tuple[dict[str, Any] | None, str]:
    """``(principal, "")`` or ``(None, reason)``; ``reason`` is safe to log (T16)."""
    tok = bearer(headers)
    if not tok:
        return None, "missing"
    try:
        claims = tokens.verify(tok, public_keys=public_keys, settings=settings,
                               audience=settings.resource_uri(path))
    except tokens.InvalidToken as exc:
        return None, str(exc)
    if config.RESOURCES[path] not in str(claims.get("scope") or "").split():
        return None, "scope"
    if not family_active(claims["sub"], claims["fid"], family_lookup):
        return None, "revoked"
    return {"sub": claims["sub"], "tenant": claims.get("tenant"),
            "groups": list(claims.get("groups") or []), "client_id": claims.get("client_id"),
            "scope": claims.get("scope"), "via": "oauth"}, ""
