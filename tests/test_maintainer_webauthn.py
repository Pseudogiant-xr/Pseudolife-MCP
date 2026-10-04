"""Every WebAuthn verification rule of the maintainer passkey flow, one
refusal each (spec 2026-10-02-maintainer-wake-design.md, "Assertion and
registration verification"; schema v54). Pure: no database."""
from __future__ import annotations

import hashlib
import json

import pytest

from pseudolife_memory.maintainer_webauthn import (
    AssertionInvalid, b64url_decode, b64url_encode, cbor_loads, cose_public_key,
    verify_assertion, verify_registration,
)
from tests.maintainer_authenticator import ORIGIN, RP_ID, RawPairs, SoftAuthenticator, cbor

CHALLENGE = hashlib.sha256(b"payload").digest()
ALGS = [-7, -8, -257]


def _register(response, *, challenge=CHALLENGE, origin=ORIGIN, rp_id=RP_ID):
    r = response["response"]
    return verify_registration(
        raw_id=b64url_decode(response["rawId"]),
        client_data_json=b64url_decode(r["clientDataJSON"]),
        attestation_object=b64url_decode(r["attestationObject"]),
        challenge=challenge, origin=origin, rp_id=rp_id)


def _assert(auth, response, *, challenge=CHALLENGE, alg=None, key=None):
    r = response["response"]
    return verify_assertion(
        public_key=auth.cose_key() if key is None else key,
        alg=auth.alg if alg is None else alg,
        client_data_json=b64url_decode(r["clientDataJSON"]),
        authenticator_data=b64url_decode(r["authenticatorData"]),
        signature=b64url_decode(r["signature"]),
        challenge=challenge, origin=ORIGIN, rp_id=RP_ID)


def _refused(check, fn, *args, **kwargs):
    with pytest.raises(AssertionInvalid) as caught:
        fn(*args, **kwargs)
    assert caught.value.check == check, caught.value.check


# ── the happy paths ────────────────────────────────────────────────────────

@pytest.mark.parametrize("alg", ALGS)
def test_each_supported_alg_registers_and_asserts(alg):
    auth = SoftAuthenticator(alg)
    reg = _register(auth.register(CHALLENGE))
    assert reg.alg == alg and reg.credential_id == auth.credential_id
    assert reg.public_key == auth.cose_key()
    parsed = _assert(auth, auth.assertion(CHALLENGE), key=reg.public_key)
    assert parsed.sign_count == 0


# ── clientDataJSON ─────────────────────────────────────────────────────────

def test_cross_origin_true_is_refused():
    auth = SoftAuthenticator()
    _refused("client_data_cross_origin", _assert, auth,
             auth.assertion(CHALLENGE, extra={"crossOrigin": True}))


def test_any_top_origin_is_refused():
    auth = SoftAuthenticator()
    _refused("client_data_top_origin", _assert, auth,
             auth.assertion(CHALLENGE, extra={"topOrigin": ORIGIN}))


def test_duplicate_json_keys_are_refused():
    auth = SoftAuthenticator()
    raw = ('{"type":"webauthn.get","challenge":"%s","origin":"%s","origin":"%s"}'
           % (b64url_encode(CHALLENGE), "https://evil.example", ORIGIN)).encode()
    _refused("client_data_duplicate_key", _assert, auth, auth.assertion(CHALLENGE, client_data=raw))


def test_wrong_type_is_refused():
    auth = SoftAuthenticator()
    raw = auth.client_data(CHALLENGE, "webauthn.create")
    _refused("client_data_type", _assert, auth, auth.assertion(CHALLENGE, client_data=raw))
    _refused("client_data_type", _register,
             auth.register(CHALLENGE, client_data=auth.client_data(CHALLENGE, "webauthn.get")))


def test_wrong_challenge_is_refused():
    auth = SoftAuthenticator()
    _refused("client_data_challenge", _assert, auth,
             auth.assertion(hashlib.sha256(b"other").digest()))


def test_wrong_origin_is_refused():
    auth = SoftAuthenticator()
    _refused("client_data_origin", _assert, auth,
             auth.assertion(CHALLENGE, origin="https://box.example.ts.net"))


def test_non_json_client_data_is_refused():
    auth = SoftAuthenticator()
    _refused("client_data_json", _assert, auth, auth.assertion(CHALLENGE, client_data=b"[1]"))


# ── authenticatorData ──────────────────────────────────────────────────────

def test_wrong_rp_id_hash_is_refused():
    auth = SoftAuthenticator()
    _refused("rp_id_hash", _assert, auth,
             auth.assertion(CHALLENGE, auth_data=auth.auth_data(rp_id="evil.example")))


@pytest.mark.parametrize("flags,check", [(0x04, "user_present"), (0x01, "user_verified")])
def test_up_and_uv_are_required(flags, check):
    auth = SoftAuthenticator()
    _refused(check, _assert, auth, auth.assertion(CHALLENGE, flags=flags))
    _refused(check, _register, auth.register(CHALLENGE, flags=flags | 0x40))


def test_registration_needs_attested_credential_data():
    auth = SoftAuthenticator()
    _refused("attested_data_missing", _register,
             auth.register(CHALLENGE, auth_data=auth.auth_data(flags=0x05)))


def test_registration_credential_id_must_equal_raw_id():
    auth = SoftAuthenticator()
    _refused("credential_id_mismatch", _register,
             auth.register(CHALLENGE, attested_id=b"another-credential"))


def test_assertion_trailing_bytes_are_refused():
    auth = SoftAuthenticator()
    _refused("auth_data_trailing", _assert, auth,
             auth.assertion(CHALLENGE, auth_data=auth.auth_data(trailing=b"\x00")))


# ── the signature ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("alg", ALGS)
def test_a_bad_signature_is_refused(alg):
    auth = SoftAuthenticator(alg)
    good = auth.assertion(CHALLENGE)
    other = SoftAuthenticator(alg)
    other.credential_id = auth.credential_id
    _refused("signature", _assert, auth, good, key=other.cose_key())


def test_the_signature_covers_the_client_data_hash():
    auth = SoftAuthenticator()
    ad = auth.auth_data()
    sig = auth.sign(ad + hashlib.sha256(b"something else").digest())
    _refused("signature", _assert, auth, auth.assertion(CHALLENGE, auth_data=ad, signature=sig))


def test_only_the_stored_alg_verifies():
    auth = SoftAuthenticator(-7)
    _refused("stored_alg", _assert, auth, auth.assertion(CHALLENGE), alg=-8)


def test_es256_signature_must_be_der():
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    auth = SoftAuthenticator(-7)
    ad = auth.auth_data()
    cdj = auth.client_data(CHALLENGE, "webauthn.get")
    der = auth.sign(ad + hashlib.sha256(cdj).digest())
    r, s = decode_dss_signature(der)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    ber = b"\x30\x81" + der[1:]   # the same SEQUENCE with a long-form length
    for bad in (raw, ber):
        _refused("signature", _assert, auth,
                 auth.assertion(CHALLENGE, auth_data=ad, client_data=cdj, signature=bad))


def test_alg_must_match_key_type_and_curve():
    auth = SoftAuthenticator(-7)
    mismatched = dict(auth.cose_map())
    mismatched[3] = -8   # an EC2 P-256 key claiming EdDSA
    _refused("cose_labels", cose_public_key, cbor(mismatched))
    wrong_curve = dict(auth.cose_map())
    wrong_curve[-1] = 2
    _refused("cose_alg_mismatch", cose_public_key, cbor(wrong_curve))
    ed = SoftAuthenticator(-8).cose_map()
    ed[1] = 2
    _refused("cose_alg_mismatch", cose_public_key, cbor(ed))


def test_pss_is_not_accepted():
    auth = SoftAuthenticator(-37)
    _refused("cose_alg", _register, auth.register(CHALLENGE))


def test_a_short_rsa_key_is_refused():
    auth = SoftAuthenticator(-257, rsa_bits=1024)
    _refused("rsa_short", _register, auth.register(CHALLENGE))


def test_an_rsa_exponent_other_than_65537_is_refused():
    auth = SoftAuthenticator(-257)
    key = auth.cose_map()
    key[-2] = (3).to_bytes(1, "big")
    _refused("rsa_exponent", cose_public_key, cbor(key))


# ── CBOR ───────────────────────────────────────────────────────────────────

def test_indefinite_length_cbor_is_refused():
    # An indefinite-length map (0xbf ... 0xff) holding the attestation fields.
    auth = SoftAuthenticator()
    good = b64url_decode(auth.register(CHALLENGE)["response"]["attestationObject"])
    indefinite = b"\xbf" + good[1:] + b"\xff"
    _refused("cbor_indefinite", cbor_loads, indefinite)
    _refused("cbor_indefinite", _register, auth.register(CHALLENGE, attestation=indefinite))


def test_trailing_cbor_bytes_are_refused():
    auth = SoftAuthenticator()
    good = b64url_decode(auth.register(CHALLENGE)["response"]["attestationObject"])
    _refused("cbor_trailing", _register, auth.register(CHALLENGE, attestation=good + b"\x00"))


def test_duplicate_cbor_keys_are_refused():
    _refused("cbor_duplicate_key", cbor_loads, cbor(RawPairs([(1, 2), (1, 3)])))
    auth = SoftAuthenticator()
    dup = cbor(RawPairs([(k, v) for k, v in auth.cose_map().items()] + [(3, -7)]))
    _refused("cbor_duplicate_key", _register, auth.register(CHALLENGE, cose=dup))


def test_an_oversized_attestation_object_is_refused():
    auth = SoftAuthenticator()
    _refused("attestation_size", _register,
             auth.register(CHALLENGE, att_stmt={"pad": b"\x00" * 4096}))


def test_cbor_tags_and_floats_are_refused():
    _refused("cbor_tag", cbor_loads, b"\xc1\x00")
    _refused("cbor_simple", cbor_loads, b"\xfa\x00\x00\x00\x00")


def test_base64url_must_be_canonical_and_unpadded():
    assert b64url_decode(b64url_encode(b"\xff\xfe")) == b"\xff\xfe"
    for bad in ("//8", "_-8=", "", "a", "AB"):  # "AB" decodes, but not canonically
        with pytest.raises(AssertionInvalid):
            b64url_decode(bad)


def test_client_data_is_hashed_as_received():
    """A whitespace variant of the same JSON is a different message: the
    signature covers the raw bytes, never a re-serialization."""
    auth = SoftAuthenticator()
    cdj = auth.client_data(CHALLENGE, "webauthn.get")
    ad = auth.auth_data()
    sig = auth.sign(ad + hashlib.sha256(cdj).digest())
    spaced = json.dumps(json.loads(cdj)).encode()
    assert spaced != cdj
    _refused("signature", _assert, auth,
             auth.assertion(CHALLENGE, auth_data=ad, client_data=spaced, signature=sig))


def test_an_assertion_carrying_attested_data_is_refused():
    auth = SoftAuthenticator()
    ad = auth.auth_data(flags=0x45, attested=auth.attested())
    _refused("attested_data_in_assertion", _assert, auth, auth.assertion(CHALLENGE, auth_data=ad))


def test_extension_data_must_be_one_cbor_map():
    auth = SoftAuthenticator()
    # A well-formed extensions map is parsed and passes.
    assert _assert(auth, auth.assertion(
        CHALLENGE, auth_data=auth.auth_data(extensions={"credProtect": 2}))).sign_count == 0
    bad = auth.auth_data(flags=0x85, trailing=cbor([1]))
    _refused("extensions", _assert, auth, auth.assertion(CHALLENGE, auth_data=bad))


@pytest.mark.parametrize("shape", [
    {"fmt": "none", "attStmt": {}},
    {"fmt": "none", "attStmt": {}, "authData": b"", "extra": 1},
    {"fmt": 1, "attStmt": {}, "authData": b""},
])
def test_the_attestation_object_has_exactly_its_three_fields(shape):
    auth = SoftAuthenticator()
    _refused("attestation_shape", _register, auth.register(CHALLENGE, attestation=cbor(shape)))


def test_a_cross_origin_false_field_is_accepted_and_an_absent_one_too():
    auth = SoftAuthenticator()
    raw = json.dumps({"type": "webauthn.get", "challenge": b64url_encode(CHALLENGE),
                      "origin": ORIGIN}).encode()
    assert _assert(auth, auth.assertion(CHALLENGE, client_data=raw)).sign_count == 0


def test_eddsa_needs_okp_and_ed25519():
    ed = SoftAuthenticator(-8).cose_map()
    ed[-1] = 7
    _refused("cose_alg_mismatch", cose_public_key, cbor(ed))


def test_a_cose_key_with_a_string_label_is_refused():
    key = dict(SoftAuthenticator(-7).cose_map())
    key["x"] = 1
    _refused("cose_shape", cose_public_key, cbor(key))
