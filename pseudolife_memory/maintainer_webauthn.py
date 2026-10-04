"""Strict WebAuthn verification for maintainer messages (schema v54).

Only the shapes the maintainer passkey flow uses: ``none`` attestation at
registration (the attestation statement is ignored, never trusted) and
assertions signed with ES256 (-7), EdDSA (-8) or RS256 (-257, PKCS#1 v1.5).
Every rule here is stricter than a general library's defaults on purpose
(spec 2026-10-02-maintainer-wake-design.md, "Assertion and registration
verification"): duplicate JSON keys, ``crossOrigin: true``, any
``topOrigin``, indefinite-length or trailing CBOR, an alg that does not match
its key type, RSA-PSS, short RSA moduli and a missing UV flag are all
refused.

Every failure raises :class:`AssertionInvalid`, whose ``check`` names the
rule that failed for the daemon log; callers answer ``assertion_invalid``
and nothing more. The signature primitives are ``cryptography``'s; the CBOR
and COSE decoding is this module's own, minimal and strict.

Pure: no storage, no configuration, no clock.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

ALG_ES256 = -7
ALG_EDDSA = -8
ALG_RS256 = -257
SUPPORTED_ALGS = (ALG_ES256, ALG_EDDSA, ALG_RS256)
MAX_ATTESTATION_BYTES = 4096
MAX_CLIENT_DATA_BYTES = 4096
MAX_CREDENTIAL_ID_BYTES = 1023
MIN_RSA_BITS = 2048
_CBOR_MAX_DEPTH = 8

FLAG_UP = 0x01
FLAG_UV = 0x04
FLAG_AT = 0x40
FLAG_ED = 0x80


class AssertionInvalid(ValueError):
    """A WebAuthn input failed verification. ``check`` is for the log only."""

    def __init__(self, check: str):
        self.check = check
        super().__init__(check)


# ── base64url ──────────────────────────────────────────────────────────────

_B64URL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(value, check: str = "base64url") -> bytes:
    """Unpadded base64url, canonical only: the decoded bytes must encode
    back to exactly ``value``, so one byte string has one spelling."""
    if not isinstance(value, str) or not value or any(c not in _B64URL for c in value):
        raise AssertionInvalid(check)
    if len(value) % 4 == 1:
        raise AssertionInvalid(check)
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        raise AssertionInvalid(check) from None
    if b64url_encode(raw) != value:
        raise AssertionInvalid(check)
    return raw


# ── strict CBOR ────────────────────────────────────────────────────────────

def _cbor_item(data: bytes, pos: int, depth: int):
    if depth > _CBOR_MAX_DEPTH:
        raise AssertionInvalid("cbor_depth")
    if pos >= len(data):
        raise AssertionInvalid("cbor_truncated")
    initial = data[pos]
    major, info = initial >> 5, initial & 0x1F
    pos += 1
    if major == 7:
        # false, true, null only: no floats, no undefined, no break.
        if info == 20:
            return False, pos
        if info == 21:
            return True, pos
        if info == 22:
            return None, pos
        raise AssertionInvalid("cbor_simple")
    if info < 24:
        value = info
    elif info in (24, 25, 26, 27):
        size = 1 << (info - 24)
        if pos + size > len(data):
            raise AssertionInvalid("cbor_truncated")
        value = int.from_bytes(data[pos:pos + size], "big")
        pos += size
    else:
        # 28-30 are reserved; 31 is an indefinite length.
        raise AssertionInvalid("cbor_indefinite")
    if major == 0:
        return value, pos
    if major == 1:
        return -1 - value, pos
    if major in (2, 3):
        if value > len(data) - pos:
            raise AssertionInvalid("cbor_truncated")
        raw = bytes(data[pos:pos + value])
        pos += value
        if major == 2:
            return raw, pos
        try:
            return raw.decode("utf-8", errors="strict"), pos
        except UnicodeDecodeError:
            raise AssertionInvalid("cbor_text") from None
    if major == 4:
        # Each item takes at least one byte: a count past the rest is a lie.
        if value > len(data) - pos:
            raise AssertionInvalid("cbor_truncated")
        items = []
        for _ in range(value):
            item, pos = _cbor_item(data, pos, depth + 1)
            items.append(item)
        return items, pos
    if major == 5:
        if value > (len(data) - pos) // 2:
            raise AssertionInvalid("cbor_truncated")
        out = {}
        for _ in range(value):
            key, pos = _cbor_item(data, pos, depth + 1)
            if type(key) not in (int, str):
                raise AssertionInvalid("cbor_key")
            if key in out:
                raise AssertionInvalid("cbor_duplicate_key")
            item, pos = _cbor_item(data, pos, depth + 1)
            out[key] = item
        return out, pos
    # Major 6: tags are not part of any shape accepted here.
    raise AssertionInvalid("cbor_tag")


def cbor_decode(data: bytes, pos: int = 0):
    """One item from ``pos``: ``(value, end)``."""
    return _cbor_item(bytes(data), pos, 0)


def cbor_loads(data: bytes):
    """Exactly one item and no trailing bytes."""
    value, end = cbor_decode(data)
    if end != len(data):
        raise AssertionInvalid("cbor_trailing")
    return value


# ── COSE keys ──────────────────────────────────────────────────────────────

_COSE_LABELS = {ALG_ES256: {1, 3, -1, -2, -3}, ALG_EDDSA: {1, 3, -1, -2},
                ALG_RS256: {1, 3, -1, -2}}


def cose_public_key(cose):
    """``(alg, public key)`` from a COSE_Key map (or its CBOR bytes). The
    alg must be one of SUPPORTED_ALGS and match the key type and curve."""
    key = cbor_loads(cose) if isinstance(cose, (bytes, bytearray)) else cose
    if not isinstance(key, dict) or any(type(k) is not int for k in key):
        raise AssertionInvalid("cose_shape")
    alg = key.get(3)
    if type(alg) is not int or alg not in SUPPORTED_ALGS:
        raise AssertionInvalid("cose_alg")
    if set(key) != _COSE_LABELS[alg]:
        raise AssertionInvalid("cose_labels")
    kty = key.get(1)
    try:
        if alg == ALG_ES256:
            x, y = key.get(-2), key.get(-3)
            if (kty != 2 or key.get(-1) != 1 or not isinstance(x, bytes)
                    or not isinstance(y, bytes) or len(x) != 32 or len(y) != 32):
                raise AssertionInvalid("cose_alg_mismatch")
            public = ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256R1(), b"\x04" + x + y)
        elif alg == ALG_EDDSA:
            x = key.get(-2)
            if kty != 1 or key.get(-1) != 6 or not isinstance(x, bytes) or len(x) != 32:
                raise AssertionInvalid("cose_alg_mismatch")
            public = ed25519.Ed25519PublicKey.from_public_bytes(x)
        else:
            n, e = key.get(-1), key.get(-2)
            if kty != 3 or not isinstance(n, bytes) or not isinstance(e, bytes):
                raise AssertionInvalid("cose_alg_mismatch")
            modulus, exponent = int.from_bytes(n, "big"), int.from_bytes(e, "big")
            if modulus.bit_length() < MIN_RSA_BITS:
                raise AssertionInvalid("rsa_short")
            if exponent != 65537:
                raise AssertionInvalid("rsa_exponent")
            public = rsa.RSAPublicNumbers(exponent, modulus).public_key()
    except AssertionInvalid:
        raise
    except (ValueError, TypeError):
        raise AssertionInvalid("cose_key") from None
    return alg, public


def _verify_signature(alg: int, public, signature: bytes, signed: bytes) -> None:
    try:
        if alg == ALG_ES256:
            # DER only. cryptography's ASN.1 parser is strict DER: a raw
            # r||s pair and a BER long-form length are both refused here
            # (checked 2026-10-02 against cryptography 48).
            decode_dss_signature(signature)
            public.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
        elif alg == ALG_EDDSA:
            public.verify(signature, signed)
        elif alg == ALG_RS256:
            public.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        else:
            raise AssertionInvalid("alg")
    except AssertionInvalid:
        raise
    except (InvalidSignature, ValueError, TypeError):
        raise AssertionInvalid("signature") from None


# ── clientDataJSON ─────────────────────────────────────────────────────────

def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise AssertionInvalid("client_data_duplicate_key")
        out[key] = value
    return out


def _refuse_constant(_value):
    raise AssertionInvalid("client_data_json")


def check_client_data(raw: bytes, *, expected_type: str, challenge: bytes,
                      origin: str) -> dict:
    """Parse the raw clientDataJSON strictly and check type, challenge,
    origin, crossOrigin and topOrigin."""
    if not isinstance(raw, (bytes, bytearray)) or not raw or len(raw) > MAX_CLIENT_DATA_BYTES:
        raise AssertionInvalid("client_data_size")
    try:
        data = json.loads(bytes(raw).decode("utf-8", errors="strict"),
                          object_pairs_hook=_no_duplicates, parse_constant=_refuse_constant)
    except AssertionInvalid:
        raise
    except (UnicodeDecodeError, ValueError):
        raise AssertionInvalid("client_data_json") from None
    if not isinstance(data, dict):
        raise AssertionInvalid("client_data_json")
    if data.get("type") != expected_type:
        raise AssertionInvalid("client_data_type")
    given = b64url_decode(data.get("challenge"), "client_data_challenge")
    if not hmac.compare_digest(given, challenge):
        raise AssertionInvalid("client_data_challenge")
    if not isinstance(data.get("origin"), str) or data["origin"] != origin:
        raise AssertionInvalid("client_data_origin")
    if "crossOrigin" in data and data["crossOrigin"] is not False:
        raise AssertionInvalid("client_data_cross_origin")
    if "topOrigin" in data:
        raise AssertionInvalid("client_data_top_origin")
    return data


# ── authenticatorData ──────────────────────────────────────────────────────

@dataclass
class AuthenticatorData:
    flags: int
    sign_count: int
    credential_id: bytes | None = None
    public_key: bytes | None = None   # the COSE_Key bytes as registered


def parse_authenticator_data(raw: bytes, *, rp_id: str, registration: bool) -> AuthenticatorData:
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 37:
        raise AssertionInvalid("auth_data_size")
    raw = bytes(raw)
    if not hmac.compare_digest(raw[:32], hashlib.sha256(rp_id.encode("utf-8")).digest()):
        raise AssertionInvalid("rp_id_hash")
    flags = raw[32]
    if not flags & FLAG_UP:
        raise AssertionInvalid("user_present")
    if not flags & FLAG_UV:
        raise AssertionInvalid("user_verified")
    sign_count = int.from_bytes(raw[33:37], "big")
    pos = 37
    out = AuthenticatorData(flags=flags, sign_count=sign_count)
    if registration:
        if not flags & FLAG_AT:
            raise AssertionInvalid("attested_data_missing")
        if len(raw) < pos + 18:
            raise AssertionInvalid("auth_data_size")
        pos += 16  # AAGUID: not used with "none" attestation
        length = int.from_bytes(raw[pos:pos + 2], "big")
        pos += 2
        if not 1 <= length <= MAX_CREDENTIAL_ID_BYTES or pos + length > len(raw):
            raise AssertionInvalid("credential_id")
        out.credential_id = raw[pos:pos + length]
        pos += length
        start = pos
        _key, pos = cbor_decode(raw, pos)
        out.public_key = raw[start:pos]
    elif flags & FLAG_AT:
        raise AssertionInvalid("attested_data_in_assertion")
    if flags & FLAG_ED:
        extensions, pos = cbor_decode(raw, pos)
        if not isinstance(extensions, dict):
            raise AssertionInvalid("extensions")
    if pos != len(raw):
        raise AssertionInvalid("auth_data_trailing")
    return out


# ── the two ceremonies ─────────────────────────────────────────────────────

def verify_assertion(*, public_key: bytes, alg: int, client_data_json: bytes,
                     authenticator_data: bytes, signature: bytes, challenge: bytes,
                     origin: str, rp_id: str) -> AuthenticatorData:
    """Verify one ``navigator.credentials.get`` result against the stored
    COSE key, with the stored alg only. Returns the parsed
    authenticatorData (its sign count is for the caller's check)."""
    check_client_data(client_data_json, expected_type="webauthn.get",
                      challenge=challenge, origin=origin)
    parsed = parse_authenticator_data(authenticator_data, rp_id=rp_id, registration=False)
    key_alg, public = cose_public_key(public_key)
    if key_alg != alg:
        raise AssertionInvalid("stored_alg")
    if not isinstance(signature, (bytes, bytearray)) or not signature:
        raise AssertionInvalid("signature")
    signed = bytes(authenticator_data) + hashlib.sha256(bytes(client_data_json)).digest()
    _verify_signature(alg, public, bytes(signature), signed)
    return parsed


@dataclass
class Registration:
    credential_id: bytes
    public_key: bytes
    alg: int
    sign_count: int


def verify_registration(*, raw_id: bytes, client_data_json: bytes, attestation_object: bytes,
                        challenge: bytes, origin: str, rp_id: str) -> Registration:
    """Verify one ``navigator.credentials.create`` result made with
    ``attestation: "none"``. The attestation statement is parsed strictly
    but never trusted: what binds the new key is the MAC'd challenge."""
    check_client_data(client_data_json, expected_type="webauthn.create",
                      challenge=challenge, origin=origin)
    if (not isinstance(attestation_object, (bytes, bytearray))
            or len(attestation_object) > MAX_ATTESTATION_BYTES):
        raise AssertionInvalid("attestation_size")
    attestation = cbor_loads(bytes(attestation_object))
    if (not isinstance(attestation, dict) or set(attestation) != {"fmt", "attStmt", "authData"}
            or not isinstance(attestation["fmt"], str)
            or not isinstance(attestation["attStmt"], dict)
            or not isinstance(attestation["authData"], bytes)):
        raise AssertionInvalid("attestation_shape")
    parsed = parse_authenticator_data(attestation["authData"], rp_id=rp_id, registration=True)
    if not hmac.compare_digest(parsed.credential_id, bytes(raw_id)):
        raise AssertionInvalid("credential_id_mismatch")
    alg, _public = cose_public_key(parsed.public_key)
    return Registration(credential_id=parsed.credential_id, public_key=parsed.public_key,
                        alg=alg, sign_count=parsed.sign_count)
