"""Access tokens: ES256 JWTs signed by a KMS key (non-exportable, T15).

Signing builds the compact JWS here (KMS returns DER; JWS wants raw r||s). Verification is
``src.dashboard.ecdsa_p256`` — pure Python, cross-checked against `cryptography` in tests —
with the algorithm pinned and every claim required (T11, T20).
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any, Callable

from src.dashboard import ecdsa_p256 as ec256
from src.dashboard.oauth import config


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_hex(value: str) -> str:
    """What we STORE for a code or refresh token — never the value itself (T1, T7)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def kid_for(public_key_der: bytes) -> str:
    return b64u(hashlib.sha256(public_key_der).digest())[:16]


def mint(claims: dict[str, Any], *, kid: str, sign_der: Callable[[bytes], bytes]) -> str:
    header = {"alg": "ES256", "typ": "at+jwt", "kid": kid}
    signing_input = (b64u(json.dumps(header, separators=(",", ":")).encode()) + "." +
                     b64u(json.dumps(claims, separators=(",", ":")).encode()))
    sig = ec256.der_to_raw(sign_der(signing_input.encode("ascii")))
    return signing_input + "." + b64u(sig)


def access_claims(*, settings: config.Settings, resource_path: str, sub: str,
                  tenant: str | None, groups: list[str], client_id: str, scope: str,
                  family_id: str, now: int) -> dict[str, Any]:
    return {"iss": settings.issuer, "aud": settings.resource_uri(resource_path), "sub": sub,
            "tenant": tenant, "groups": list(groups or []), "client_id": client_id,
            "scope": scope, "fid": family_id, "iat": now, "nbf": now,
            "exp": now + config.ACCESS_TTL_S, "jti": random_token(16)}


def jwk(public_key_der: bytes) -> dict[str, str]:
    x, y = ec256.spki_point(public_key_der)
    return {"kty": "EC", "crv": "P-256", "use": "sig", "alg": "ES256",
            "kid": kid_for(public_key_der),
            "x": b64u(x.to_bytes(32, "big")), "y": b64u(y.to_bytes(32, "big"))}


class InvalidToken(Exception):
    """Why a bearer token is unacceptable — an enum-ish string safe to log (T16)."""


def verify(token: str, *, public_keys: dict[str, bytes], settings: config.Settings,
           audience: str, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    parts = str(token or "").split(".")
    if len(parts) != 3:
        raise InvalidToken("malformed")
    try:
        header = json.loads(_unb64u(parts[0]))
        claims = json.loads(_unb64u(parts[1]))
        sig = _unb64u(parts[2])
    except (ValueError, TypeError):
        raise InvalidToken("malformed") from None
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise InvalidToken("malformed")
    if header.get("alg") != "ES256":
        raise InvalidToken("alg")
    der = public_keys.get(str(header.get("kid") or ""))
    if der is None:
        raise InvalidToken("kid")
    if not ec256.verify(ec256.spki_point(der), (parts[0] + "." + parts[1]).encode("ascii"), sig):
        raise InvalidToken("signature")
    for k in ("exp", "iat", "nbf", "iss", "aud", "sub", "fid", "scope"):
        if k not in claims:
            raise InvalidToken("claims")
    if claims["iss"] != settings.issuer:
        raise InvalidToken("iss")
    if claims["aud"] != audience:
        raise InvalidToken("aud")
    try:
        if float(claims["exp"]) + config.LEEWAY_S < now:
            raise InvalidToken("expired")
        if float(claims["nbf"]) - config.LEEWAY_S > now:
            raise InvalidToken("nbf")
    except (TypeError, ValueError):
        raise InvalidToken("claims") from None
    return claims


def pkce_s256(verifier: str) -> str:
    return b64u(hashlib.sha256(verifier.encode("ascii")).digest())


der_to_raw = ec256.der_to_raw   # KMS DER → JWS raw (kept here for callers/tests)
