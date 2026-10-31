"""The signed-action comparator checks crypto before normalizing its bytes."""
from __future__ import annotations

import copy
import hashlib
import hmac
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from board import scenarios as S  # noqa: E402
from board.maintainer import ORIGIN, RP_ID, SECRET, SignedNormalizer, auth  # noqa: E402
from board.norm import Windows  # noqa: E402
from pseudolife_memory.maintainer_webauthn import b64url_encode  # noqa: E402
from pseudolife_memory.storage.maintainer import canonical, challenge_bytes  # noqa: E402

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
    norm.value(value)
    assert norm.problems


def proof():
    payload = issued()["payload"]
    signer = auth(S.Ctx())
    response = signer.assertion(challenge_bytes(payload))["response"]
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
    norm.value(bad)
    assert norm.problems


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
