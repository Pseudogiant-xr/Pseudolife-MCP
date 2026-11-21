"""Check the signed board fixtures against their original WebAuthn bytes."""
import base64
import hashlib
import hmac
import json


def canonical(fields):
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def challenge_bytes(payload):
    return hashlib.sha256(payload.encode("utf-8")).digest()


def _unique_object(pairs):
    obj = dict(pairs)
    if len(pairs) != len(obj):
        raise ValueError("duplicate JSON key")
    return obj


def _nonfinite(token):
    raise ValueError(f"nonfinite JSON value: {token}")


def _json_object(raw):
    obj = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_nonfinite)
    if not isinstance(obj, dict):
        raise ValueError("expected a JSON object")
    return obj


def payload_fields(payload):
    if not isinstance(payload, str):
        raise ValueError("maintainer payload is not text")
    fields = _json_object(payload)
    if canonical(fields) != payload:
        raise ValueError("maintainer payload is not canonical")
    return fields


def verify_challenge(value, secret):
    from tests.maintainer_authenticator import b64url_encode

    payload = value.get("payload")
    payload_fields(payload)
    expected = hmac.digest(secret, payload.encode("utf-8"), "sha256").hex()
    mac = value.get("mac")
    if not isinstance(mac, str) or not hmac.compare_digest(expected.encode(), mac.encode()):
        raise ValueError("maintainer response MAC does not authenticate its payload")
    if "publicKey" in value:
        options = value["publicKey"]
        if not isinstance(options, dict) or options.get("challenge") != b64url_encode(challenge_bytes(payload)):
            raise ValueError("WebAuthn options do not challenge the signed payload")


def _decode(value):
    from tests.maintainer_authenticator import b64url_encode

    if not isinstance(value, str) or not value:
        raise ValueError("expected nonempty base64url")
    raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if b64url_encode(raw) != value:
        raise ValueError("noncanonical base64url")
    return raw


def verify_proof(value, public_key, *, origin, rp_id):
    """Verify one of the fixture's Ed25519 assertions, with no extension data."""
    from cryptography.exceptions import InvalidSignature
    from tests.maintainer_authenticator import b64url_encode

    payload = value.get("payload")
    payload_fields(payload)
    client_bytes = _decode(value.get("client_data_json"))
    authenticator = _decode(value.get("authenticator_data"))
    signature = _decode(value.get("signature"))

    if len(client_bytes) > 4096:
        raise ValueError("client data is too large")
    client = _json_object(client_bytes.decode("utf-8"))
    if client.get("type") != "webauthn.get" or client.get("origin") != origin:
        raise ValueError("unexpected assertion type or origin")
    if client.get("crossOrigin", False) is not False or "topOrigin" in client:
        raise ValueError("cross-origin assertion")
    if client.get("challenge") != b64url_encode(challenge_bytes(payload)):
        raise ValueError("assertion does not challenge the signed payload")

    if len(authenticator) != 37 or authenticator[:32] != hashlib.sha256(rp_id.encode()).digest():
        raise ValueError("unexpected authenticator data size or RP ID")
    flags = authenticator[32]
    if flags & 0x05 != 0x05 or flags & 0xC0:
        raise ValueError("unexpected assertion flags")
    try:
        public_key.verify(signature, authenticator + hashlib.sha256(client_bytes).digest())
    except InvalidSignature as exc:
        raise ValueError("invalid Ed25519 signature") from exc
    return client
