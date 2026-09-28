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
import json
import secrets
import time
from typing import Any
from urllib.parse import urlparse

from src.dashboard.ecdsa_p256 import _N, _b64u, _unb64u, public_key, sign  # noqa: F401


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
