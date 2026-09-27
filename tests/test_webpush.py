"""#167: the pure-Python ES256 VAPID signer, verified against the `cryptography` package."""
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.dashboard import webpush as wp

crypto = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature  # noqa: E402


def _pub(d):
    raw = wp.public_key(d)
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)


def test_public_key_matches_cryptography():
    d = wp.load_private_key(wp.generate_private_key())
    ref = ec.derive_private_key(d, ec.SECP256R1()).public_key()
    assert _pub(d).public_numbers() == ref.public_numbers()


def test_signatures_verify():
    d = wp.load_private_key(wp.generate_private_key())
    for msg in (b"", b"hello", b"x" * 1000):
        sig = wp.sign(d, msg)
        r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
        _pub(d).verify(encode_dss_signature(r, s), msg, ec.ECDSA(hashes.SHA256()))  # raises if bad


def test_vapid_header_is_a_valid_es256_jwt_for_the_push_origin():
    d = wp.load_private_key(wp.generate_private_key())
    h = wp.vapid_headers("https://fcm.googleapis.com/fcm/send/abc", d, subject="mailto:x@y.z")["Authorization"]
    t = h.split("t=")[1].split(",")[0]
    head, body, sig = t.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=="))
    assert claims["aud"] == "https://fcm.googleapis.com" and claims["sub"] == "mailto:x@y.z"
    raw = base64.urlsafe_b64decode(sig + "==")
    _pub(d).verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                   f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))


def test_send_is_payload_free():
    d = wp.load_private_key(wp.generate_private_key())
    seen = {}

    class R:
        status_code = 201
    status = wp.send("https://push.example/x", d, subject="mailto:a@b.c",
                     post=lambda url, headers, data, timeout: seen.update(h=headers, d=data) or R())
    assert status == 201 and seen["d"] == b"" and seen["h"]["Content-Length"] == "0"
