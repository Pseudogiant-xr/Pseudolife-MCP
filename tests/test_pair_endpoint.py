"""``POST /api/pair`` and the HTTP gate's view of stored principals (spec
2026-10-02, Part 2b: "Redemption endpoint", "One resolver, every site",
"What an invited principal can do").

The endpoint is unauthenticated by design, so it refuses anything a browser
can send (an ``Origin`` header, a non-JSON body), caps the body at 1 KiB,
answers every refusal with the same bytes, and holds pairing at 429 after
20 failed redemptions a minute without consulting the store. Redemption
itself is replaced here by a fake; tests/test_principal_store_pg.py covers
the real one.
"""
from __future__ import annotations

import json
import logging
import threading

import pytest

from pseudolife_memory import principal_store
from pseudolife_memory.principal_store import PrincipalSnapshot, StoredPrincipal
from pseudolife_memory.principals import install_store, installed_store, secret_sha256
from pseudolife_memory.web.api import build_console_app
from pseudolife_memory.web.fixtures import FixtureService
from tests.asgi_helpers import call, call_with_headers, stub_mcp

ENV_TOKEN = "fixture-env-token"
STORED_TOKEN = "fixture-stored-token"
CODE = "ABCD-EFGH-JKMN"
CODE_HASH = secret_sha256("ABCDEFGHJKMN")
TOKEN_HASH = secret_sha256(STORED_TOKEN)
JSON = (b"content-type", b"application/json")


def _body(code=CODE, token_sha256=TOKEN_HASH, **extra) -> bytes:
    return json.dumps({"code": code, "token_sha256": token_sha256, **extra}).encode()


@pytest.fixture
def snapshot():
    previous = installed_store()
    snap = PrincipalSnapshot()
    snap.refresh(lambda: ([], "0123456789abcdef"))
    install_store(snap)
    yield snap
    install_store(previous)


@pytest.fixture
def redeemed(monkeypatch):
    """A fake redemption: ``calls`` records each call, ``result`` is what it
    returns (a row, None for a refusal, or an exception to raise)."""
    state = {"calls": [], "result": StoredPrincipal("laptop", TOKEN_HASH, "core", True, False),
             "threads": []}

    def fake(dsn, code_hash, token_hash):
        state["calls"].append((code_hash, token_hash))
        state["threads"].append(threading.current_thread().name)
        result = state["result"]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(principal_store, "redeem", fake)
    return state


def _app(*, token=ENV_TOKEN, service=None):
    return build_console_app(stub_mcp, token, lambda: {}, service or FixtureService())


def _pair(app, body=None, headers=None):
    return call_with_headers(app, "POST", "/api/pair",
                             headers=[JSON] if headers is None else headers,
                             body=_body() if body is None else body)


# -- redemption ----------------------------------------------------------------

def test_a_redeemed_code_answers_principal_tier_and_bank_and_the_token_works_at_once(
        snapshot, redeemed):
    app = _app()
    status, headers, body = _pair(app)
    assert status == 200
    assert json.loads(body) == {"principal": "laptop", "tier": "core", "bank": "0123456789abcdef"}
    assert headers[b"cache-control"] == b"no-store"
    assert redeemed["calls"] == [(CODE_HASH, TOKEN_HASH)]
    # Never on the event loop.
    assert redeemed["threads"] and redeemed["threads"][0] != threading.main_thread().name
    status, _ = call(app, "GET", "/api/stats",
                     headers=[(b"authorization", f"Bearer {STORED_TOKEN}".encode())])
    assert status == 200


@pytest.mark.parametrize("body", [
    _body(code="not-a-code"), _body(code=""), _body(code=None), _body(code=12),
    _body(token_sha256="ab" * 31), _body(token_sha256="AB" * 32), _body(token_sha256=None),
    _body(extra="x"), json.dumps({"code": CODE}).encode(), b"[]", b"null", b"not json",
    b"", b"\xff\xfe", json.dumps({"code": CODE, "token": STORED_TOKEN}).encode(),
], ids=lambda b: repr(b)[:30])
def test_every_malformed_request_gets_the_same_refusal(snapshot, redeemed, body):
    status, _, out = _pair(_app(), body=body)
    assert (status, out) == (400, b'{"error": "pairing_refused"}')
    assert redeemed["calls"] == []


def test_an_unknown_expired_or_used_code_gets_the_same_refusal(snapshot, redeemed):
    redeemed["result"] = None
    status, _, out = _pair(_app())
    assert (status, out) == (400, b'{"error": "pairing_refused"}')


def test_an_open_daemon_refuses_pairing(snapshot, redeemed):
    status, _, out = _pair(_app(token=None))
    assert (status, out) == (400, b'{"error": "pairing_refused"}')
    assert redeemed["calls"] == []


def test_a_daemon_without_a_store_refuses_pairing(redeemed):
    previous = installed_store()
    install_store(None)
    try:
        status, _, out = _pair(_app())
    finally:
        install_store(previous)
    assert (status, out) == (400, b'{"error": "pairing_refused"}')
    service = FixtureService()
    service._db_url = None
    snap = PrincipalSnapshot()
    install_store(snap)
    try:
        status, _, out = _pair(_app(service=service))
    finally:
        install_store(previous)
    assert (status, out) == (400, b'{"error": "pairing_refused"}')
    assert redeemed["calls"] == []


def test_an_origin_header_is_refused_before_anything_else(snapshot, redeemed):
    status, _, out = _pair(_app(), headers=[JSON, (b"origin", b"http://127.0.0.1:8765")])
    assert status == 403 and json.loads(out) == {"error": "forbidden_origin"}
    assert redeemed["calls"] == []


@pytest.mark.parametrize("ctype", [None, b"text/plain", b"application/x-www-form-urlencoded",
                                   b"multipart/form-data"])
def test_only_json_is_accepted(snapshot, redeemed, ctype):
    headers = [] if ctype is None else [(b"content-type", ctype)]
    status, _, _ = _pair(_app(), headers=headers)
    assert status == 415
    assert redeemed["calls"] == []


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "OPTIONS"])
def test_only_post(snapshot, redeemed, method):
    status, _ = call(_app(), method, "/api/pair", headers=[JSON], body=_body())
    assert status == 405
    assert redeemed["calls"] == []


def test_the_body_is_capped_at_one_kibibyte(snapshot, redeemed):
    status, _, _ = _pair(_app(), body=_body(pad="x" * 1024))
    assert status == 413
    assert redeemed["calls"] == []


def test_twenty_failures_a_minute_hold_pairing_at_429_without_the_store(snapshot, redeemed):
    app = _app()
    redeemed["result"] = None
    for _ in range(20):
        assert _pair(app)[0] == 400
    assert len(redeemed["calls"]) == 20
    redeemed["result"] = StoredPrincipal("laptop", TOKEN_HASH, None, True, False)
    status, _, out = _pair(app)
    assert status == 429 and json.loads(out) == {"error": "rate_limited"}
    assert len(redeemed["calls"]) == 20
    # Malformed requests count too.
    other = _app()
    for _ in range(20):
        assert _pair(other, body=b"junk")[0] == 400
    assert _pair(other)[0] == 429


def test_the_budget_frees_after_a_minute(snapshot, redeemed, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(principal_store.time, "monotonic", lambda: clock[0])
    app = _app()
    for _ in range(20):
        _pair(app, body=b"junk")
    assert _pair(app)[0] == 429
    clock[0] += 60.0
    assert _pair(app)[0] == 200


def test_a_database_failure_is_unavailable_and_logs_only_its_type(snapshot, redeemed, caplog):
    redeemed["result"] = RuntimeError(f"connection to {CODE_HASH} with {STORED_TOKEN} failed")
    with caplog.at_level(logging.DEBUG):
        status, _, out = _pair(_app())
    assert status == 503 and json.loads(out) == {"error": "pairing_unavailable"}
    assert "RuntimeError" in caplog.text
    assert CODE_HASH not in caplog.text and STORED_TOKEN not in caplog.text


def test_nothing_from_the_body_reaches_the_logs(snapshot, redeemed, caplog):
    app = _app()
    with caplog.at_level(logging.DEBUG):
        for body in (_body(), _body(code="ZZZZ-ZZZZ-ZZZZ"), b"{\"code\": \"QQQQ-QQQQ-QQQQ\"",
                     _body(extra="fixture-extra-value")):
            _pair(app, body=body)
        redeemed["result"] = None
        _pair(app)
    for secret in (CODE, "ABCDEFGHJKMN", CODE_HASH, TOKEN_HASH, "ZZZZ", "QQQQ",
                   "fixture-extra-value"):
        assert secret not in caplog.text


# -- the gate ------------------------------------------------------------------

def _stored(snapshot, name="laptop", token=STORED_TOKEN, **kw):
    snapshot.add(StoredPrincipal(name, secret_sha256(token), kw.get("tier"), kw.get("board", True),
                                 False))


def _auth(token):
    return [(b"authorization", f"Bearer {token}".encode()), JSON]


@pytest.mark.parametrize("path", ["/api/config", "/api/daemon-notice"])
def test_a_stored_principal_is_refused_the_operator_routes(snapshot, path):
    _stored(snapshot)
    status, out = call(_app(), "POST", path, headers=_auth(STORED_TOKEN), body=b"{}")
    assert status == 403 and json.loads(out) == {"error": "operator_principal_required"}


@pytest.mark.parametrize("path", ["/api/config", "/api/daemon-notice"])
def test_an_environment_principal_keeps_the_operator_routes(snapshot, path):
    _stored(snapshot)
    status, out = call(_app(), "POST", path, headers=_auth(ENV_TOKEN), body=b"{}")
    assert status != 403 or json.loads(out).get("error") != "operator_principal_required"


def test_a_stored_principal_still_reads_the_config(snapshot):
    _stored(snapshot)
    status, _ = call(_app(), "GET", "/api/config", headers=_auth(STORED_TOKEN))
    assert status == 200


def test_the_api_binds_the_stored_principal(snapshot):
    from pseudolife_memory.writer_context import request_principal
    _stored(snapshot)
    service = FixtureService()
    service.stats = lambda: {"principal": request_principal()}
    status, out = call(_app(service=service), "GET", "/api/stats", headers=_auth(STORED_TOKEN))
    assert status == 200 and json.loads(out)["principal"] == "laptop"


def test_the_board_rest_sees_the_stored_principal(snapshot, monkeypatch):
    from pseudolife_memory.web.coordination import CoordinationHub
    _stored(snapshot)
    seen = []

    async def handle(self, action, body, headers, principal):
        seen.append(principal)
        return {"ok": True}

    monkeypatch.setattr(CoordinationHub, "handle", handle)
    status, _ = call(_app(), "POST", "/api/coordination/receive", headers=_auth(STORED_TOKEN),
                     body=b"{}")
    assert status == 200 and seen == ["laptop"]


def test_an_open_install_still_refuses_the_board_to_a_stored_bearer(snapshot):
    _stored(snapshot)
    status, out = call(_app(token=None), "POST", "/api/coordination/register",
                       headers=_auth(STORED_TOKEN), body=b"{}")
    assert status == 401 and json.loads(out)["error"] == "authentication_required"


def test_an_unknown_bearer_is_401(snapshot):
    status, _ = call(_app(), "GET", "/api/stats", headers=_auth("fixture-random"))
    assert status == 401


# -- the snapshot cannot be checked --------------------------------------------

@pytest.fixture
def unloaded():
    previous = installed_store()
    snap = PrincipalSnapshot()
    install_store(snap)
    yield snap
    install_store(previous)


@pytest.mark.parametrize("method,path", [("GET", "/api/stats"), ("POST", "/mcp"),
                                         ("POST", "/api/hook/session-end"),
                                         ("POST", "/api/coordination/receive")])
def test_a_non_environment_bearer_gets_503_while_the_view_is_unavailable(unloaded, method, path):
    status, out = call(_app(), method, path, headers=_auth(STORED_TOKEN), body=b"{}")
    assert status == 503 and json.loads(out) == {"error": "principals_unavailable"}


def test_the_environment_still_works_while_the_view_is_unavailable(unloaded):
    status, _ = call(_app(), "GET", "/api/stats", headers=_auth(ENV_TOKEN))
    assert status == 200
    status, _ = call(_app(), "GET", "/api/stats")
    assert status == 401


@pytest.mark.parametrize("path", ["/api/hook/memory-changes", "/api/hook/park-gate"])
def test_the_always_200_hooks_treat_unavailable_as_unauthorized(unloaded, path):
    status, out = call(_app(), "GET", path, headers=_auth(STORED_TOKEN), query="since=0")
    assert status == 200 and out in (b"", b"allow\n")


def test_the_board_check_in_names_the_reason(unloaded):
    service = FixtureService()
    service.config.coordination.enabled = True
    status, headers, out = call_with_headers(_app(service=service), "GET",
                                             "/api/hook/coordination-start",
                                             headers=_auth(STORED_TOKEN))
    assert status == 200 and out == b""
    assert headers[b"x-pl-board"] == b"off; reason=principals_unavailable"
