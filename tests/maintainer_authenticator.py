"""A software WebAuthn authenticator for the maintainer passkey tests.

Builds authenticatorData, clientDataJSON, attestation objects and
assertions byte for byte, with ES256, EdDSA or RS256 keys generated in the
test, and lets a test break any one field. Never used outside tests.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

from pseudolife_memory.maintainer_webauthn import b64url_encode

RP_ID = "console.example.com"
ORIGIN = "https://console.example.com:8443"


def cbor(value) -> bytes:
    """Definite-length CBOR for the shapes the tests build."""
    def head(major, n):
        if n < 24:
            return bytes([(major << 5) | n])
        for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 1 << (8 * size):
                return bytes([(major << 5) | info]) + n.to_bytes(size, "big")
        raise ValueError(n)
    if value is False:
        return b"\xf4"
    if value is True:
        return b"\xf5"
    if value is None:
        return b"\xf6"
    if isinstance(value, int):
        return head(0, value) if value >= 0 else head(1, -1 - value)
    if isinstance(value, bytes):
        return head(2, len(value)) + value
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return head(3, len(raw)) + raw
    if isinstance(value, list):
        return head(4, len(value)) + b"".join(cbor(v) for v in value)
    if isinstance(value, dict):
        return head(5, len(value)) + b"".join(cbor(k) + cbor(v) for k, v in value.items())
    if isinstance(value, RawPairs):
        return head(5, len(value.pairs)) + b"".join(cbor(k) + cbor(v) for k, v in value.pairs)
    raise TypeError(type(value))


class RawPairs:
    """A CBOR map written pair by pair, so a test can repeat a key."""

    def __init__(self, pairs):
        self.pairs = pairs


class SoftAuthenticator:
    def __init__(self, alg=-7, *, rp_id=RP_ID, origin=ORIGIN, rsa_bits=2048, sign_count=0):
        self.alg, self.rp_id, self.origin = alg, rp_id, origin
        self.credential_id = os.urandom(16)
        self.sign_count = sign_count
        if alg == -7:
            self.private = ec.generate_private_key(ec.SECP256R1())
        elif alg == -8:
            self.private = ed25519.Ed25519PrivateKey.generate()
        elif alg in (-257, -37):
            self.private = rsa.generate_private_key(public_exponent=65537, key_size=rsa_bits)
        else:
            raise ValueError(alg)

    @property
    def id(self) -> str:
        return b64url_encode(self.credential_id)

    def cose_map(self) -> dict:
        public = self.private.public_key()
        if self.alg == -7:
            numbers = public.public_numbers()
            return {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"),
                    -3: numbers.y.to_bytes(32, "big")}
        if self.alg == -8:
            from cryptography.hazmat.primitives import serialization
            raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            return {1: 1, 3: -8, -1: 6, -2: raw}
        numbers = public.public_numbers()
        n = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
        e = numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
        return {1: 3, 3: self.alg, -1: n, -2: e}

    def cose_key(self) -> bytes:
        return cbor(self.cose_map())

    def client_data(self, challenge: bytes, kind: str, *, origin=None, extra=None,
                    raw=None) -> bytes:
        if raw is not None:
            return raw
        data = {"type": kind, "challenge": b64url_encode(challenge),
                "origin": self.origin if origin is None else origin, "crossOrigin": False}
        data.update(extra or {})
        return json.dumps(data, separators=(",", ":")).encode("utf-8")

    def auth_data(self, *, flags=0x05, sign_count=None, rp_id=None, attested=None,
                  extensions=None, trailing=b"") -> bytes:
        rp_hash = hashlib.sha256((self.rp_id if rp_id is None else rp_id).encode()).digest()
        if extensions is not None:
            flags |= 0x80
        count = self.sign_count if sign_count is None else sign_count
        out = rp_hash + bytes([flags]) + struct.pack(">I", count)
        if attested is not None:
            out += attested
        if extensions is not None:
            out += cbor(extensions)
        return out + trailing

    def attested(self, *, credential_id=None, cose=None) -> bytes:
        cid = self.credential_id if credential_id is None else credential_id
        key = self.cose_key() if cose is None else cose
        return b"\x00" * 16 + struct.pack(">H", len(cid)) + cid + key

    def register(self, challenge: bytes, *, flags=0x45, client_data=None, attestation=None,
                 auth_data=None, raw_id=None, fmt="none", att_stmt=None, cose=None,
                 attested_id=None, origin=None, extra=None) -> dict:
        cdj = client_data if client_data is not None else self.client_data(
            challenge, "webauthn.create", origin=origin, extra=extra)
        if attestation is None:
            ad = auth_data if auth_data is not None else self.auth_data(
                flags=flags, attested=self.attested(credential_id=attested_id, cose=cose))
            attestation = cbor({"fmt": fmt, "attStmt": {} if att_stmt is None else att_stmt,
                                "authData": ad})
        rid = self.credential_id if raw_id is None else raw_id
        return {"id": b64url_encode(rid), "rawId": b64url_encode(rid), "type": "public-key",
                "response": {"clientDataJSON": b64url_encode(cdj),
                             "attestationObject": b64url_encode(attestation)}}

    def sign(self, data: bytes) -> bytes:
        if self.alg == -7:
            return self.private.sign(data, ec.ECDSA(hashes.SHA256()))
        if self.alg == -8:
            return self.private.sign(data)
        if self.alg == -37:
            return self.private.sign(data, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                       salt_length=32), hashes.SHA256())
        return self.private.sign(data, padding.PKCS1v15(), hashes.SHA256())

    def assertion(self, challenge: bytes, *, flags=0x05, client_data=None, auth_data=None,
                  signature=None, bump=True, sign_count=None, origin=None, extra=None,
                  credential_id=None) -> dict:
        if bump and sign_count is None and self.sign_count:
            self.sign_count += 1
        cdj = client_data if client_data is not None else self.client_data(
            challenge, "webauthn.get", origin=origin, extra=extra)
        ad = auth_data if auth_data is not None else self.auth_data(
            flags=flags, sign_count=sign_count)
        sig = signature if signature is not None else self.sign(
            ad + hashlib.sha256(cdj).digest())
        cid = self.credential_id if credential_id is None else credential_id
        return {"id": b64url_encode(cid), "rawId": b64url_encode(cid), "type": "public-key",
                "response": {"clientDataJSON": b64url_encode(cdj),
                             "authenticatorData": b64url_encode(ad),
                             "signature": b64url_encode(sig)}}
