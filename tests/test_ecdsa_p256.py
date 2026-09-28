"""#181: the pure-Python P-256 helpers the OAuth tokens rely on, cross-checked against
the `cryptography` package (signatures made BY cryptography, as KMS would, must verify)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.dashboard import ecdsa_p256 as ec256

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402


def _key():
    k = ec.generate_private_key(ec.SECP256R1())
    spki = k.public_key().public_bytes(serialization.Encoding.DER,
                                       serialization.PublicFormat.SubjectPublicKeyInfo)
    return k, spki


def test_verify_accepts_cryptography_signatures_via_der_to_raw():
    k, spki = _key()
    pt = ec256.spki_point(spki)
    for msg in (b"", b"a.b", b"x" * 2000):
        der = k.sign(msg, ec.ECDSA(hashes.SHA256()))          # what KMS Sign returns
        assert ec256.verify(pt, msg, ec256.der_to_raw(der))


def test_verify_rejects_tampering_and_wrong_key():
    k, spki = _key()
    pt = ec256.spki_point(spki)
    raw = ec256.der_to_raw(k.sign(b"msg", ec.ECDSA(hashes.SHA256())))
    assert not ec256.verify(pt, b"msg2", raw)
    assert not ec256.verify(pt, b"msg", raw[:-1] + bytes([raw[-1] ^ 1]))
    assert not ec256.verify(pt, b"msg", b"\x00" * 64)
    _, other = _key()
    assert not ec256.verify(ec256.spki_point(other), b"msg", raw)


def test_own_signatures_verify_both_ways():
    d = 0x1234567890ABCDEF
    pt = ec256.point_of(d)
    sig = ec256.sign(d, b"hello")
    assert ec256.verify(pt, b"hello", sig)


def test_spki_point_rejects_non_p256():
    k = ec.generate_private_key(ec.SECP384R1())
    spki = k.public_key().public_bytes(serialization.Encoding.DER,
                                       serialization.PublicFormat.SubjectPublicKeyInfo)
    with pytest.raises(ValueError):
        ec256.spki_point(spki)
