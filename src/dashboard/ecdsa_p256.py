"""ECDSA P-256 / ES256 in plain Python — shared by Web Push VAPID (#167) and the OAuth access
tokens of the MCP authorization server (#181).

The shared Lambda bundle carries no crypto library, and the volumes here (a few signatures or
verifications per request) make speed irrelevant. Signing uses RFC 6979 deterministic nonces
(no RNG), verification is the textbook check. ``tests/test_ecdsa_p256.py`` cross-checks every
function against the ``cryptography`` package, including signatures made by it.

Private keys for OAuth never live here: KMS signs (non-exportable key) and returns a DER
signature — ``der_to_raw`` converts it; ``spki_point`` reads KMS's public key for JWKS/verify.
"""
from __future__ import annotations

import base64
import hashlib
import hmac

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


def verify(point: tuple[int, int], msg: bytes, sig: bytes) -> bool:
    """ES256 verification of a raw r||s signature against public point ``(x, y)``."""
    if len(sig) != 64 or not _on_curve(point):
        return False
    r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    if not (1 <= r < _N and 1 <= s < _N):
        return False
    e = int.from_bytes(hashlib.sha256(msg).digest(), "big")
    w = pow(s, -1, _N)
    pt = _add(_mul(e * w % _N, _G), _mul(r * w % _N, point))
    return pt is not None and pt[0] % _N == r


def _on_curve(p: tuple[int, int]) -> bool:
    x, y = p
    return 0 <= x < _P and 0 <= y < _P and (y * y - (x * x * x + _A * x + _B)) % _P == 0


def point_of(d: int) -> tuple[int, int]:
    return _mul(d, _G)


def der_to_raw(der: bytes) -> bytes:
    """ASN.1 DER ECDSA-Sig-Value (what KMS Sign returns) → JWS raw r||s."""
    def _int(buf: bytes, i: int) -> tuple[int, int]:
        if buf[i] != 0x02:
            raise ValueError("DER: expected INTEGER")
        n = buf[i + 1]
        return int.from_bytes(buf[i + 2:i + 2 + n], "big"), i + 2 + n
    if not der or der[0] != 0x30:
        raise ValueError("DER: expected SEQUENCE")
    i = 2 if der[1] < 0x80 else 2 + (der[1] & 0x7F)
    r, i = _int(der, i)
    s, _ = _int(der, i)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def spki_point(spki_der: bytes) -> tuple[int, int]:
    """(x, y) from a DER SubjectPublicKeyInfo for P-256 (KMS GetPublicKey)."""
    raw = spki_der[-65:]
    if raw[0] != 0x04 or b"\x2a\x86\x48\xce\x3d\x03\x01\x07" not in spki_der:
        raise ValueError("not an uncompressed P-256 public key")
    pt = (int.from_bytes(raw[1:33], "big"), int.from_bytes(raw[33:], "big"))
    if not _on_curve(pt):
        raise ValueError("point not on P-256")
    return pt


def public_key(d: int) -> bytes:
    """Uncompressed SEC1 point (65 bytes) — the browser's ``applicationServerKey``."""
    x, y = _mul(d, _G)
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


