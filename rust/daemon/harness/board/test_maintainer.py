"""The signed-action comparator checks crypto before normalizing its bytes."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from board import scenarios as S  # noqa: E402
from board.maintainer import ORIGIN, RP_ID, SECRET, SignedNormalizer, auth  # noqa: E402
from board.maintainer_proofs import canonical, challenge_bytes  # noqa: E402
from board.norm import Windows  # noqa: E402
from tests.maintainer_authenticator import b64url_encode  # noqa: E402

NOW = 1_790_000_000.0


def normalizer():
    windows = Windows()
    windows.steps = [(NOW, NOW + 0.5)]
    return SignedNormalizer(windows)


def issued(nonce="a" * 32):
    payload = canonical({"purpose": "send", "nonce": nonce, "expires_at": NOW + 120,
                         "text": "Inspect the fixture.", "urgent": False, "to": "b" * 32})
    return {"payload": payload,
            "mac": hmac.new(bytes.fromhex(SECRET), payload.encode(), hashlib.sha256).hexdigest(),
            "publicKey": {"rpId": RP_ID, "userVerification": "required",
                          "challenge": b64url_encode(challenge_bytes(payload))}}


@pytest.mark.parametrize("corrupt", ["mac", "challenge", "canonical"])
def test_bad_challenge_material_cannot_normalize_clean(corrupt):
    value = issued()
    if corrupt == "mac":
        value["mac"] = "0" * 64
    elif corrupt == "challenge":
        value["publicKey"]["challenge"] = b64url_encode(b"\0" * 32)
    else:
        value["payload"] += " "
        value["mac"] = hmac.new(bytes.fromhex(SECRET), value["payload"].encode(), hashlib.sha256).hexdigest()
        value["publicKey"]["challenge"] = b64url_encode(challenge_bytes(value["payload"]))
    norm = normalizer()
    norm.learn(value)
    assert norm.value(value) == value
    assert norm.problems


def test_issued_challenge_payload_must_be_text():
    value = dict(issued(), payload=42)
    norm = normalizer()
    assert norm.value(value) == value
    assert norm.problems == ["maintainer payload is not text"]


def proof(**assertion_options):
    payload = issued()["payload"]
    signer = auth(S.Ctx())
    response = signer.assertion(challenge_bytes(payload), **assertion_options)["response"]
    return {"credential_id": signer.id, "label": "Primary fixture", "payload": payload,
            "signed_at": NOW, "signature": response["signature"],
            "client_data_json": response["clientDataJSON"],
            "authenticator_data": response["authenticatorData"]}


def test_proof_signatures_are_verified_before_normalization():
    good = proof()
    norm = normalizer()
    norm.learn(good)
    normalized = norm.value(good)
    assert normalized["signature"] == "<verified signature>"
    assert normalized["client_data_json"]["origin"] == ORIGIN
    assert not norm.problems
    bad = copy.deepcopy(good)
    bad["signature"] = b64url_encode(b"\0" * 64)
    assert norm.value(bad) == bad
    assert norm.problems


def test_signature_is_verified_over_the_original_client_data_bytes():
    value = proof(client_data=_client_data(indent=2))
    norm = normalizer()
    assert norm.value(value)["signature"] == "<verified signature>"
    assert not norm.problems


def _client_data(*, indent=None, **fields):
    base = {"origin": ORIGIN, "challenge": b64url_encode(challenge_bytes(issued()["payload"])),
            "type": "webauthn.get", "crossOrigin": False}
    return json.dumps({**base, **fields}, indent=indent).encode("utf-8")


@pytest.mark.parametrize("options,reason", [
    pytest.param({"flags": 0x01}, "assertion flags", id="missing-uv"),
    pytest.param({"flags": 0x45}, "assertion flags", id="attested-flag"),
    pytest.param({"auth_data": hashlib.sha256(RP_ID.encode()).digest() + b"\x05\0\0\0"},
                 "authenticator data size", id="short-authenticator"),
    pytest.param({"origin": "https://other.example.com"}, "type or origin", id="wrong-origin"),
    pytest.param({"client_data": _client_data(type="webauthn.create")}, "type or origin", id="wrong-type"),
    pytest.param({"client_data": _client_data(crossOrigin=True)}, "cross-origin", id="cross-origin"),
    pytest.param({"client_data": _client_data(topOrigin=ORIGIN)}, "cross-origin", id="top-origin"),
    pytest.param({"client_data": _client_data(challenge="wrong")}, "does not challenge", id="wrong-challenge"),
    pytest.param({"client_data": _client_data(extra="x" * 4096)}, "too large", id="oversize-client-data"),
    pytest.param({"client_data": _client_data(extra=float("nan"))}, "nonfinite", id="nonfinite-client-data"),
    pytest.param({"client_data": b'{"origin":null,' + _client_data()[1:]}, "duplicate JSON key", id="duplicate-key"),
    pytest.param({"client_data": b"[]"}, "expected a JSON object", id="non-object-client-data"),
])
def test_valid_signature_does_not_hide_wrong_proof_fields(options, reason):
    value = proof(**options)
    norm = normalizer()
    assert norm.value(value) == value
    assert any(reason in problem for problem in norm.problems)


def test_a_signature_for_another_relying_party_is_refused():
    signer = auth(S.Ctx())
    value = proof(auth_data=signer.auth_data(rp_id="other.example.com"))
    norm = normalizer()
    assert norm.value(value) == value
    assert any("RP ID" in problem for problem in norm.problems)


@pytest.mark.parametrize("field,reason", [
    (None, "expected nonempty base64url"),
    ("credential_id", "unknown proof credential"),
    ("signature", "noncanonical base64url"),
])
def test_missing_or_noncanonical_stored_proof_material_is_refused(field, reason):
    value = proof()
    if field is None:
        del value["signature"]
    elif field == "credential_id":
        value[field] = "unknown"
    else:
        value[field] += "=="
    norm = normalizer()
    assert norm.value(value, "maintainer_proof") == value
    assert any(reason in problem for problem in norm.problems)


def test_signed_fixture_checks_work_with_the_frozen_package_blocked(monkeypatch):
    import tests
    from rust.harness_support.import_blocker import cold_harness_imports

    value, stored_proof = issued(), proof()
    with cold_harness_imports(), monkeypatch.context() as patch:
        patch.delitem(sys.modules, "tests.maintainer_authenticator", raising=False)
        patch.delattr(tests, "maintainer_authenticator", raising=False)
        from board import maintainer

        windows = Windows()
        windows.steps = [(NOW, NOW + 0.5)]
        norm = maintainer.SignedNormalizer(windows)
        assert norm.value(value)["mac"] == "<verified MAC>"
        norm.learn(stored_proof)
        assert norm.value(stored_proof)["signature"] == "<verified signature>"
        assert not norm.problems

        ctx = maintainer.S.Ctx()
        ctx.vals["sample"] = value
        request = maintainer.complete("sample", "send").body(ctx)
        assert request["payload"] == value["payload"]
        assert request["assertion"]["id"] == stored_proof["credential_id"]


def test_challenges_keep_all_signed_fields_while_nonce_and_clock_vary():
    left, right = issued("a" * 32), issued("c" * 32)
    a, b = normalizer(), normalizer()
    a.learn(left)
    b.learn(right)
    assert a.value(left) == b.value(right)
    assert not a.problems and not b.problems
    right["payload"] = right["payload"].replace("Inspect", "Ignore")
    right["mac"] = hmac.new(bytes.fromhex(SECRET), right["payload"].encode(), hashlib.sha256).hexdigest()
    right["publicKey"]["challenge"] = b64url_encode(challenge_bytes(right["payload"]))
    assert a.value(left) != b.value(right)


@pytest.mark.parametrize("key,offset", [("active_from", 86400), ("flagged_at", 0),
                                       ("signed_at", 0), ("at", 0)])
def test_maintainer_times_require_the_declared_clock_window(key, offset):
    norm = normalizer()
    assert norm.value(NOW + offset, key) == f"T0+{offset}"
    norm.value(NOW + offset + 5, key)
    assert norm.problems


def test_runner_registers_all_six_actions_and_compares_maintainer_tables():
    from board import run
    from board.maintainer import SCENARIOS, TABLES

    routes = set()
    for name, scenario in SCENARIOS.items():
        assert run.S.SCENARIOS[name] is scenario
        assert scenario.extra_tables == TABLES
        routes.update(step.path for step in scenario().steps() if isinstance(step.path, str))
    assert {"/api/maintainer/" + route for route in
            ("enrol", "role", "send", "cancel", "repudiate", "revoke")} <= routes


def test_refusal_fixtures_preserve_explicit_old_timestamps():
    from board.maintainer_refusals import FixtureNormalizer

    windows = Windows()
    windows.steps = [(NOW, NOW + 0.5)]
    norm = FixtureNormalizer(windows)
    assert norm.value(1, "created_at") == 1
    assert norm.value(1, "expires_at") == 1
    assert norm.value(NOW + 120, "expires_at") == "T0+120"
    norm.value(2, "expires_at")
    assert norm.problems


def test_additional_refusal_scenarios_are_selectable():
    from board import run
    from board.maintainer_refusals import SCENARIOS

    assert len(SCENARIOS) == 6
    for name, scenario in SCENARIOS.items():
        assert run.S.SCENARIOS[name] is scenario
        assert any(hasattr(step, "check") for step in scenario().steps())
