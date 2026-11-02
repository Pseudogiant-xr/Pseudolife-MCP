"""The HTTP side of Console passkey sign-in (schema v59; spec 2026-11-02,
section 5): the pre-auth challenge route, the passkey body of ``POST
/api/console/session``, ``POST /api/console/passkey-bind`` and ``/health``.
The verification itself is replaced by fakes here;
tests/test_console_passkey_pg.py drives it with a software authenticator.
"""
from __future__ import annotations

import json

import pytest

from pseudolife_memory import console_passkey
from pseudolife_memory.principals import secret_sha256
from pseudolife_memory.storage.maintainer import MaintainerError
from pseudolife_memory.web.fixtures import FixtureService
from tests.asgi_helpers import call
from tests.test_console_signin import (  # noqa: F401  (fixtures)
    ENV_TOKEN, JSON, SESSION_TOKEN, _app, _auth, _session, sessions, snapshot,
)

PASSKEY_ORIGIN = "https://bank.example.com"


def _service():
    service = FixtureService()
    m = service.config.coordination.maintainer
    m.rp_id, m.origin = "bank.example.com", PASSKEY_ORIGIN
    service.config.coordination.enabled = True
    return service


def _challenge(app, origin=PASSKEY_ORIGIN, body=None):
    headers = [JSON] + ([(b"origin", origin.encode())] if origin else [])
    body = body if body is not None else json.dumps(
        {"token_sha256": secret_sha256(SESSION_TOKEN), "label": "Firefox"}).encode()
    status, out = call(app, "POST", "/api/console/passkey-challenge", headers=headers, body=body)
    return status, json.loads(out)


@pytest.fixture
def issued(monkeypatch):
    state = {"calls": [], "result": {"payload": "{}", "mac": "0" * 64, "publicKey": {}}}

    def fake(dsn, *, token_hash, label, rp_id):
        state["calls"].append((token_hash, label, rp_id))
        return state["result"]

    monkeypatch.setattr(console_passkey, "issue_signin_challenge", fake)
    return state


def test_a_challenge_is_issued_at_the_passkey_origin_only(snapshot, sessions, issued):
    app, _ = _app(service=_service())
    status, out = _challenge(app)
    assert status == 200 and out["mac"] == "0" * 64
    assert issued["calls"] == [(secret_sha256(SESSION_TOKEN), "Firefox", "bank.example.com")]
    assert _challenge(app, origin="http://127.0.0.1:8765")[0] == 403
    assert _challenge(app, origin=None)[0] == 403
    assert len(issued["calls"]) == 1


def test_no_challenge_without_passkeys_or_signing_keys(snapshot, sessions, issued):
    app, _ = _app()
    status, out = _challenge(app, origin="http://127.0.0.1:8765")
    assert status == 409 and out["error"] == "passkey_signin_unavailable"
    issued["result"] = None
    app, _ = _app(service=_service())
    status, out = _challenge(app)
    assert status == 409 and "maintainer setup" in out["hint"]


@pytest.mark.parametrize("body", [b"{}", b"[]", b"not json",
                                  json.dumps({"token_sha256": "x", "label": "y"}).encode(),
                                  json.dumps({"token_sha256": "a" * 64, "label": "y",
                                              "extra": 1}).encode()])
def test_a_malformed_challenge_request_is_refused(snapshot, sessions, issued, body):
    app, _ = _app(service=_service())
    assert _challenge(app, body=body)[0] == 400
    assert issued["calls"] == []


@pytest.fixture
def completed(monkeypatch):
    state = {"calls": [], "result": "session"}

    def fake(service, body, *, principal_source):
        state["calls"].append((body, principal_source("default")))
        if isinstance(state["result"], Exception):
            raise state["result"]
        return _session(method="passkey", issuer="credential-1", sid="pk1")

    monkeypatch.setattr(console_passkey, "complete_signin", fake)
    return state


def _passkey_signin(app, origin=PASSKEY_ORIGIN, body=None):
    body = body if body is not None else json.dumps(
        {"payload": "{}", "mac": "0" * 64, "assertion": {"id": "x", "pad": "y" * 3000}}).encode()
    status, out = call(app, "POST", "/api/console/session",
                       headers=[JSON, (b"origin", origin.encode())], body=body)
    return status, json.loads(out)


def test_a_passkey_assertion_signs_the_browser_in(snapshot, sessions, completed):
    sessions.refresh(lambda: ([], ["credential-1"]))
    app, _ = _app(service=_service())
    status, out = _passkey_signin(app)
    assert status == 200 and out["session_id"] == "pk1"
    assert completed["calls"][0][1] == "environment"
    status, _ = call(app, "GET", "/api/stats", headers=_auth(SESSION_TOKEN))
    assert status == 200


def test_a_passkey_body_elsewhere_than_the_passkey_origin_is_refused(snapshot, sessions,
                                                                    completed):
    app, _ = _app(service=_service())
    status, out = _passkey_signin(app, origin="http://127.0.0.1:8765")
    assert (status, out) == (400, {"error": "signin_refused"}) and completed["calls"] == []


@pytest.mark.parametrize("code,expected", [
    ("assertion_invalid", (400, "signin_refused")), ("challenge_spent", (400, "signin_refused")),
    ("challenge_expired", (400, "signin_refused")),
    ("passkey_signin_unavailable", (409, "passkey_signin_unavailable")),
    ("coordination_unavailable", (503, "signin_unavailable")),
])
def test_each_refusal_maps_to_what_the_console_needs(snapshot, sessions, completed, code,
                                                     expected):
    completed["result"] = MaintainerError(code)
    app, _ = _app(service=_service())
    status, out = _passkey_signin(app)
    assert (status, out["error"]) == expected
    if code == "passkey_signin_unavailable":
        assert "Use this passkey to sign in" in out["hint"]


def test_a_code_body_keeps_its_one_kib_limit_and_a_passkey_body_gets_eight(snapshot, sessions,
                                                                          completed):
    app, _ = _app(service=_service())
    big_code = json.dumps({"code": "ABCD-EFGH-JKMN", "token_sha256": "a" * 64,
                           "label": "x" * 1500}).encode()
    assert _passkey_signin(app, body=big_code)[0] == 413
    too_big = json.dumps({"payload": "x" * 9000, "mac": "0" * 64, "assertion": {}}).encode()
    assert _passkey_signin(app, body=too_big)[0] == 413


# -- POST /api/console/passkey-bind ------------------------------------------------

def test_binding_runs_as_the_callers_principal_and_signs_out_the_keys_other_sessions(
        snapshot, sessions, monkeypatch):
    sessions.refresh(lambda: ([_session("other-browser", principal="laptop", source="store",
                                        method="passkey", issuer="credential-1", sid="pk2")],
                              ["credential-1"]))
    seen = {}

    def fake(service, body, principal, *, is_owner):
        seen.update(principal=principal, owner=is_owner(principal),
                    maintainer=is_owner("maintainer"))
        return {"credential_id": "credential-1", "signs_in_as": principal}

    monkeypatch.setattr(console_passkey, "bind", fake)
    app, _ = _app(service=_service())
    status, out = call(app, "POST", "/api/console/passkey-bind", headers=_auth(ENV_TOKEN),
                       body=b"{}")
    assert status == 200 and seen == {"principal": "default", "owner": True,
                                      "maintainer": False}
    assert sessions.lookup(secret_sha256("other-browser")) is None


def test_a_bind_refusal_keeps_its_maintainer_code(snapshot, sessions, monkeypatch):
    def fake(service, body, principal, *, is_owner):
        raise MaintainerError("principal_not_allowed")
    monkeypatch.setattr(console_passkey, "bind", fake)
    app, _ = _app(service=_service())
    status, out = call(app, "POST", "/api/console/passkey-bind", headers=_auth(ENV_TOKEN),
                       body=b"{}")
    assert (status, json.loads(out)["error"]) == (403, "principal_not_allowed")


# -- /health --------------------------------------------------------------------------

def test_health_offers_the_passkey_where_passkeys_are_set_up(sessions):
    from pseudolife_memory.daemon import _build_health_payload
    service = _service()
    service._db_url = "postgresql://fixture/unused"
    assert _build_health_payload(service, True)["console_signin"] == {"link": True,
                                                                      "passkey": True}
    service.config.coordination.maintainer.origin = ""
    assert _build_health_payload(service, True)["console_signin"]["passkey"] is False
