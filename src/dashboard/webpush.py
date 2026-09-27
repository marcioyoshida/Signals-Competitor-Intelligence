"""Payload-free Web Push with VAPID (RFC 8292) — #167.

Onça's push messages carry NO payload by policy (the lock screen never shows a finding), so
there is nothing to encrypt (RFC 8291 applies only to a message body). What remains is the
VAPID header: an ES256-signed JWT proving the application server's identity to the push
service. ES256 = ECDSA over P-256 with SHA-256; it is implemented here in plain Python because
the shared Lambda bundle carries no crypto library, and the volume (a few pushes an hour) makes
speed irrelevant. The nonce is RFC 6979-deterministic, so signing needs no RNG at all.

`tests/test_webpush.py` verifies signatures produced here with the `cryptography` package.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any
from urllib.parse import urlparse

# NIST P-256 (secp256r1)
_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
      0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _add(p: tuple[int, int] | None, q: tuple[int, int] | None) -> tuple[int, int] | None:
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0] and (p[1] + q[1]) % _P == 0:
        return None
    if p == q:
        lam = (3 * p[0] * p[0] + _A) * pow(2 * p[1], -1, _P) % _P
    else:
        lam = (q[1] - p[1]) * pow(q[0] - p[0], -1, _P) % _P
    x = (lam * lam - p[0] - q[0]) % _P
    return x, (lam * (p[0] - x) - p[1]) % _P


def _mul(k: int, p: tuple[int, int]) -> tuple[int, int]:
    r, q = None, p
    while k:
        if k & 1:
            r = _add(r, q)
        q = _add(q, q)
        k >>= 1
    assert r is not None
    return r


def _rfc6979_k(d: int, h: bytes) -> int:
    x = d.to_bytes(32, "big")
    h1 = (int.from_bytes(h, "big") % _N).to_bytes(32, "big")
    v, k = b"\x01" * 32, b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        cand = int.from_bytes(v, "big")
        if 1 <= cand < _N:
            return cand
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign(d: int, msg: bytes) -> bytes:
    """ES256 signature (raw r||s, 64 bytes) of ``msg`` with private scalar ``d``."""
    h = hashlib.sha256(msg).digest()
    e = int.from_bytes(h, "big")
    while True:
        k = _rfc6979_k(d, h)
        r = _mul(k, _G)[0] % _N
        s = pow(k, -1, _N) * (e + r * d) % _N
        if r and s:
            return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def public_key(d: int) -> bytes:
    """Uncompressed SEC1 point (65 bytes) — the browser's ``applicationServerKey``."""
    x, y = _mul(d, _G)
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


def generate_private_key() -> str:
    """A new VAPID private key as base64url(32-byte scalar). Store it as a SecureString."""
    return _b64u((secrets.randbelow(_N - 1) + 1).to_bytes(32, "big"))


def load_private_key(b64: str) -> int:
    d = int.from_bytes(_unb64u(b64.strip()), "big")
    if not 1 <= d < _N:
        raise ValueError("invalid VAPID private key")
    return d


def vapid_headers(endpoint: str, d: int, *, subject: str, ttl_s: int = 12 * 3600) -> dict[str, str]:
    """The Authorization header for one push service (aud = the endpoint's origin)."""
    u = urlparse(endpoint)
    claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + ttl_s, "sub": subject}
    head = _b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = _b64u(json.dumps(claims, separators=(",", ":")).encode())
    sig = _b64u(sign(d, f"{head}.{body}".encode()))
    return {"Authorization": f"vapid t={head}.{body}.{sig}, k={_b64u(public_key(d))}"}


def send(endpoint: str, d: int, *, subject: str, urgency: str = "high",
         ttl: int = 86400, post: Any = None) -> int:
    """POST a payload-free push. Returns the push service's HTTP status (201 = accepted;
    404/410 = the subscription is gone and must be pruned)."""
    if post is None:
        import requests

        post = requests.post
    headers = {**vapid_headers(endpoint, d, subject=subject), "TTL": str(ttl), "Urgency": urgency,
               "Content-Length": "0"}
    r = post(endpoint, headers=headers, data=b"", timeout=10)
    return int(r.status_code)
