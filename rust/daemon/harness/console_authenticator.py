"""Deterministic Ed25519 WebAuthn assertions for the Console fixture only."""
import base64
import hashlib
import json
import struct

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class ConsoleAuthenticator:
    def __init__(self, name, rp_id, origin):
        seed = hashlib.sha256(("console-passkey-fixture:" + name).encode()).digest()
        self.private = Ed25519PrivateKey.from_private_bytes(seed)
        self.credential_id, self.rp_id, self.origin = seed[:16], rp_id, origin
        self.sign_count = 1

    @property
    def id(self):
        return b64(self.credential_id)

    def cose_key(self):
        public = self.private.public_key().public_bytes(serialization.Encoding.Raw,
                                                       serialization.PublicFormat.Raw)
        # Definite CBOR map {1: 1, 3: -8, -1: 6, -2: <32-byte public key>}.
        return bytes.fromhex("a4010103272006215820") + public

    def assertion(self, challenge, *, origin=None, flags=0x05, sign_count=None,
                  bump=True, signature=None):
        if bump and sign_count is None and self.sign_count:
            self.sign_count += 1
        client = json.dumps({"type": "webauthn.get", "challenge": b64(challenge),
                             "origin": self.origin if origin is None else origin,
                             "crossOrigin": False}, separators=(",", ":")).encode()
        auth = hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + struct.pack(
            ">I", self.sign_count if sign_count is None else sign_count)
        signed = self.private.sign(auth + hashlib.sha256(client).digest()) if signature is None else signature
        return {"id": self.id, "rawId": self.id, "type": "public-key",
                "response": {"clientDataJSON": b64(client), "authenticatorData": b64(auth),
                             "signature": b64(signed)}}
