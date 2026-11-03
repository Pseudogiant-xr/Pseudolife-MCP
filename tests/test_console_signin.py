"""Console sign-in with a one-time link (schema v59; spec 2026-11-02,
sections 3 and 4): the session resolver, where it applies and where it must
not, the operator allowlist, and the three routes.

A Console session is a bearer the browser minted; the daemon stores only
its hash. It resolves only at the call sites that opt in: the ``/api``
transport gate (except the board's mailbox routes, the sign-in link and the
daemon notice) and the board snapshot and maintainer routes behind it.
Hooks and ``/mcp`` never resolve one, so a session is never an agent
credential. Database operations are replaced by fakes here;
tests/test_console_sessions_pg.py covers the real ones.
"""
from __future__ import annotations

import json
import time

import pytest

from pseudolife_memory import console_sessions
from pseudolife_memory.console_sessions import (
    ConsoleSession, SessionSnapshot, environment_issuers, install_sessions, installed_sessions,
    normalize_origin, resolve,
)
from pseudolife_memory.principal_store import PrincipalSnapshot, StoredPrincipal
from pseudolife_memory.principals import (
    PrincipalsUnavailable, install_store, installed_store, secret_sha256,
)
from pseudolife_memory.web.api import build_console_app
from pseudolife_memory.web.fixtures import FixtureService
from tests.asgi_helpers import call, call_with_headers, stub_mcp

ENV_TOKEN = "fixture-env-token"
STORED_TOKEN = "fixture-stored-token"
SESSION_TOKEN = "fixture-browser-session-token"
STORE_SESSION_TOKEN = "fixture-browser-session-from-paired"
JSON = (b"content-type", b"application/json")
ORIGIN = "http://127.0.0.1:8765"
CODE = "ABCD-EFGH-JKMN"


def _session(token=SESSION_TOKEN, *, principal="default", source="environment",
             issuer=None, method="link", created=None, last_used=None, sid="s1"):
    now = time.time()
    created = now - 60 if created is None else created
    return ConsoleSession(
        session_id=sid, token_hash=secret_sha256(token), principal=principal, source=source,
        issuer=secret_sha256(ENV_TOKEN) if issuer is None else issuer, method=method,
        label="Firefox on Linux", created_at=created, last_used_at=last_used,
        expires_at=created + console_sessions.SESSION_MAX_S)


@pytest.fixture
def snapshot():
    previous = installed_store()
    snap = PrincipalSnapshot()
    snap.refresh(lambda: ([], "0123456789abcdef"))
    install_store(snap)
    yield snap
    install_store(previous)


@pytest.fixture
def sessions():
    previous = installed_sessions()
    snap = SessionSnapshot()
    snap.refresh(lambda: ([], []))
    install_sessions(snap)
    yield snap
    install_sessions(previous)


def _stored(snapshot, name="laptop", token=STORED_TOKEN):
    snapshot.add(StoredPrincipal(name, secret_sha256(token), None, True, False))


def _store_session(sessions, snapshot):
    _stored(snapshot)
    sessions.add(_session(STORE_SESSION_TOKEN, principal="laptop", source="store",
                          issuer=secret_sha256(STORED_TOKEN), sid="s2"))


def _app(*, token=ENV_TOKEN, service=None):
    service = service or FixtureService()
    service._db_url = "postgresql://fixture/unused"
    return build_console_app(stub_mcp, token, lambda: {}, service), service


def _auth(token):
    return [(b"authorization", f"Bearer {token}".encode()), JSON]


# -- origins ---------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("http://127.0.0.1:8765", "http://127.0.0.1:8765"),
    ("http://127.0.0.1:8765/", "http://127.0.0.1:8765"),
    ("HTTP://LOCALHOST:8765", "http://localhost:8765"),
    ("https://bank.example.com:443", "https://bank.example.com"),
    ("http://localhost:80", "http://localhost"),
    ("https://bank.example.com:8443", "https://bank.example.com:8443"),
    ("http://[::1]:8765", "http://[::1]:8765"),
])
def test_an_origin_is_normalised_as_a_browser_sends_it(value, expected):
    assert normalize_origin(value) == expected


@pytest.mark.parametrize("value", [
    None, "", "ftp://host", "http://", "http://host/ui", "http://host?x=1", "http://host#x",
    "http://user:pw@host", "http://host:99999", "http://ho st", "127.0.0.1:8765", 8765,
])
def test_anything_but_a_plain_origin_is_refused(value):
    assert normalize_origin(value) is None


# -- the resolver ---------------------------------------------------------------

def _resolve(sessions, token=SESSION_TOKEN, *, env=None, names=("default",), store=None,
             now=None):
    issuers = environment_issuers({}, ENV_TOKEN) if env is None else env
    return resolve(f"Bearer {token}", sessions=sessions, env_issuers=issuers,
                   env_principals=set(names), store=store, now=now)


def test_a_session_an_environment_token_issued_resolves_to_its_principal(sessions):
    sessions.add(_session())
    hit = _resolve(sessions)
    assert hit is not None and (hit.principal, hit.issued_by) == ("default", "environment")


def test_a_session_ends_when_its_issuing_token_changes(sessions):
    sessions.add(_session())
    assert _resolve(sessions, env=environment_issuers({}, "a-rotated-token")) is None


def test_a_session_ends_when_its_principal_leaves_the_environment(sessions):
    sessions.add(_session(principal="pc", issuer=secret_sha256("pc-token")))
    env = environment_issuers({"pc-token": "pc"}, None)
    assert _resolve(sessions, env=env, names={"pc"}) is not None
    assert _resolve(sessions, env=env, names=set()) is None


def test_a_session_ends_thirty_days_after_its_last_use(sessions):
    now = time.time()
    sessions.add(_session(created=now - 40 * 86400, last_used=now - 31 * 86400))
    assert _resolve(sessions, now=now) is None


def test_a_used_session_lives_past_thirty_days_but_never_past_ninety(sessions):
    now = time.time()
    sessions.add(_session(created=now - 60 * 86400, last_used=now - 86400))
    assert _resolve(sessions, now=now) is not None
    sessions.drop(["s1"])
    sessions.add(_session(created=now - 91 * 86400, last_used=now - 60))
    assert _resolve(sessions, now=now) is None


def test_a_use_is_noted_and_extends_the_session(sessions):
    now = time.time()
    sessions.add(_session(created=now - 40 * 86400, last_used=now - 29 * 86400))
    assert _resolve(sessions, now=now) is not None
    assert sessions.take_uses() == {"s1": now}
    # Two days on, the stored stamp alone would be 31 days old.
    assert _resolve(sessions, now=now + 2 * 86400) is not None


def test_a_revoked_session_stops_resolving_at_once(sessions):
    sessions.add(_session())
    sessions.drop(["s1"])
    assert _resolve(sessions) is None


def test_a_dropped_session_stays_dropped_through_an_older_refresh(sessions):
    row = _session()

    def load():
        sessions.drop(["s1"])        # a revocation commits while this read runs
        return [row], []

    sessions.refresh(load)
    assert _resolve(sessions) is None


def test_a_stale_session_view_is_unavailable_not_unauthorized():
    snap = SessionSnapshot(max_age=60.0, clock=lambda: 1000.0)
    snap.refresh(lambda: ([_session()], []))
    snap._clock = lambda: 2000.0
    with pytest.raises(PrincipalsUnavailable):
        _resolve(snap)


def test_a_session_a_paired_machine_issued_needs_its_row(sessions, snapshot):
    _store_session(sessions, snapshot)
    hit = _resolve(sessions, STORE_SESSION_TOKEN, store=snapshot)
    assert hit is not None and (hit.principal, hit.issued_by) == ("laptop", "store")
    # Re-paired with a new token: the old token no longer names it.
    snapshot.add(StoredPrincipal("laptop", secret_sha256("a-new-token"), None, True, False))
    assert _resolve(sessions, STORE_SESSION_TOKEN, store=snapshot) is None


def test_a_session_a_paired_machine_issued_is_unavailable_while_the_store_is_stale(sessions):
    store = PrincipalSnapshot(max_age=60.0, clock=lambda: 0.0)
    store.refresh(lambda: ([StoredPrincipal("laptop", secret_sha256(STORED_TOKEN), None, True,
                                            False)], None))
    store._clock = lambda: 1000.0
    sessions.add(_session(STORE_SESSION_TOKEN, principal="laptop", source="store",
                          issuer=secret_sha256(STORED_TOKEN), sid="s2"))
    with pytest.raises(PrincipalsUnavailable):
        _resolve(sessions, STORE_SESSION_TOKEN, store=store)


def test_a_passkey_session_needs_its_key_active(sessions):
    sessions.add(_session(method="passkey", issuer="credential-1"))
    assert _resolve(sessions) is None
    sessions.refresh(lambda: ([_session(method="passkey", issuer="credential-1")],
                              ["credential-1"]))
    assert _resolve(sessions) is not None


def test_no_session_snapshot_resolves_nothing():
    assert _resolve(None) is None


# -- where a session resolves, and where it must not -----------------------------

def test_a_session_reads_the_api(snapshot, sessions):
    sessions.add(_session())
    app, _ = _app()
    status, _ = call(app, "GET", "/api/stats", headers=_auth(SESSION_TOKEN))
    assert status == 200


@pytest.mark.parametrize("method,path", [
    ("POST", "/mcp"),
    ("POST", "/api/coordination/receive"),
    ("POST", "/api/coordination/register"),
    ("POST", "/api/daemon-notice"),
    ("POST", "/api/hook/session-end"),
])
def test_a_session_is_not_an_agent_credential(snapshot, sessions, method, path):
    sessions.add(_session())
    app, _ = _app()
    status, out = call(app, method, path, headers=_auth(SESSION_TOKEN), body=b"{}")
    assert status == 401, out


@pytest.mark.parametrize("path", ["/api/hook/coordination-start", "/api/hook/park-gate"])
def test_the_hooks_treat_a_session_as_unauthorized(snapshot, sessions, path):
    sessions.add(_session())
    app, _ = _app()
    status, headers, out = call_with_headers(app, "GET", path, headers=_auth(SESSION_TOKEN),
                                             query="agent=a1")
    assert status == 200 and out == b""


def test_the_same_requests_with_the_environment_token_still_work(snapshot, sessions):
    app, _ = _app()
    status, headers, _ = call_with_headers(app, "GET", "/api/hook/coordination-start",
                                           headers=_auth(ENV_TOKEN))
    assert status == 200 and headers[b"x-pl-board"] != b"off; reason=unauthorized"


def test_a_session_cannot_issue_a_session(snapshot, sessions):
    sessions.add(_session())
    app, _ = _app()
    status, out = call(app, "POST", "/api/console/link", headers=_auth(SESSION_TOKEN),
                       body=json.dumps({"origin": ORIGIN}).encode())
    assert status == 403 and json.loads(out)["error"] == "session_cannot_issue"


# -- the operator allowlist -------------------------------------------------------

def test_a_session_an_environment_token_issued_may_change_settings(snapshot, sessions):
    sessions.add(_session())
    app, _ = _app()
    status, out = call(app, "POST", "/api/config", headers=_auth(SESSION_TOKEN), body=b"{}")
    assert not (status == 403 and json.loads(out).get("error") == "operator_principal_required")


def test_a_session_a_paired_machine_issued_may_not_change_settings(snapshot, sessions):
    _store_session(sessions, snapshot)
    app, _ = _app()
    status, out = call(app, "POST", "/api/config", headers=_auth(STORE_SESSION_TOKEN),
                       body=b"{}")
    assert status == 403 and json.loads(out) == {"error": "operator_principal_required"}
    # It reads them, as its machine does.
    status, _ = call(app, "GET", "/api/config", headers=_auth(STORE_SESSION_TOKEN))
    assert status == 200


def test_the_operator_gate_stays_a_literal_path_set():
    # The Rust harness parses this declaration (rust/daemon/harness/principals.py).
    import ast
    import inspect

    from pseudolife_memory.web import api
    tree = ast.parse(inspect.getsource(api))
    found = [node for node in tree.body if isinstance(node, ast.Assign)
             and any(getattr(t, "id", None) == "_OPERATOR_POST_PATHS" for t in node.targets)]
    assert len(found) == 1
    assert ast.literal_eval(found[0].value.args[0]) == {"/api/config", "/api/daemon-notice"}


# -- POST /api/console/link -------------------------------------------------------

@pytest.fixture
def issued(monkeypatch):
    state = {"calls": []}

    def fake(dsn, *, principal, source, issuer, origin):
        state["calls"].append({"principal": principal, "source": source, "issuer": issuer,
                               "origin": origin})
        return {"code": "ABCDEFGHJKMN", "expires_at": 1234.5}

    monkeypatch.setattr(console_sessions, "issue_code", fake)
    return state


def _link(app, token=ENV_TOKEN, **body):
    status, out = call(app, "POST", "/api/console/link", headers=_auth(token),
                       body=json.dumps({"origin": ORIGIN, **body}).encode())
    return status, json.loads(out)


def test_a_link_for_a_loopback_console(snapshot, sessions, issued):
    app, _ = _app()
    status, out = _link(app)
    assert status == 200
    assert out["url"] == f"{ORIGIN}/ui/#/signin?code={CODE}"
    assert (out["code"], out["origin"], out["principal"]) == (CODE, ORIGIN, "default")
    assert out["expires_in"] == 300
    # Stored under the loopback key, so any loopback alias redeems it.
    assert issued["calls"] == [{"principal": "default", "source": "environment",
                                "issuer": secret_sha256(ENV_TOKEN),
                                "origin": "http://localhost:8765"}]


def test_a_link_can_land_on_a_page(snapshot, sessions, issued):
    app, _ = _app()
    status, out = _link(app, to="board")
    assert status == 200 and out["url"].endswith("&to=board")
    assert _link(app, to="../evil")[1]["error"] == "invalid_route"


def test_a_paired_machine_gets_a_link_for_its_own_principal(snapshot, sessions, issued):
    _stored(snapshot)
    app, _ = _app()
    status, out = _link(app, STORED_TOKEN)
    assert status == 200 and out["principal"] == "laptop"
    assert issued["calls"][0]["source"] == "store"
    assert issued["calls"][0]["issuer"] == secret_sha256(STORED_TOKEN)


def test_with_passkeys_set_up_the_link_is_for_the_consoles_https_address(
        snapshot, sessions, issued):
    service = FixtureService()
    service.config.coordination.maintainer.rp_id = "bank.example.com"
    service.config.coordination.maintainer.origin = "https://bank.example.com"
    app, _ = _app(service=service)
    status, out = _link(app, origin="http://100.64.0.1:8765")
    assert status == 200
    assert out["url"].startswith("https://bank.example.com/ui/#/signin?code=")
    assert issued["calls"][0]["origin"] == "https://bank.example.com"


def test_without_passkeys_a_remote_address_is_refused_with_its_fix(snapshot, sessions, issued):
    app, _ = _app()
    status, out = _link(app, origin="http://100.64.0.1:8765")
    assert status == 409 and out["error"] == "signin_origin_unsupported"
    assert "maintainer setup" in out["hint"] and issued["calls"] == []


@pytest.mark.parametrize("body", [{"origin": "http://127.0.0.1:8765/ui"}, {"origin": None},
                                  {"origin": "javascript:alert(1)"}])
def test_a_bad_origin_is_refused(snapshot, sessions, issued, body):
    app, _ = _app()
    status, out = call(app, "POST", "/api/console/link", headers=_auth(ENV_TOKEN),
                       body=json.dumps(body).encode())
    assert status == 400 and json.loads(out)["error"] == "invalid_origin"


def test_an_open_daemon_offers_no_sign_in(snapshot, sessions, issued):
    app, _ = _app(token=None)
    status, out = call(app, "POST", "/api/console/link", headers=[JSON],
                       body=json.dumps({"origin": ORIGIN}).encode())
    assert status == 409 and json.loads(out)["error"] == "signin_unavailable"
    assert issued["calls"] == []


def test_a_daemon_without_a_bank_offers_no_sign_in(snapshot, issued):
    previous = installed_sessions()
    install_sessions(None)
    try:
        app, _ = _app()
        status, out = _link(app)
    finally:
        install_sessions(previous)
    assert status == 409 and out["error"] == "signin_unavailable"


# -- POST /api/console/session -----------------------------------------------------

@pytest.fixture
def redeemed(monkeypatch):
    state = {"calls": [], "result": "session"}

    def fake(dsn, code_hash, token_hash, *, origin, label, still_valid):
        state["calls"].append({"code_hash": code_hash, "token_hash": token_hash,
                               "origin": origin, "label": label,
                               "valid": still_valid("default", "environment",
                                                    secret_sha256(ENV_TOKEN))})
        if state["result"] == "session":
            return _session(sid="new1")
        return state["result"]

    monkeypatch.setattr(console_sessions, "redeem_code", fake)
    return state


def _signin_body(**over):
    body = {"code": CODE, "token_sha256": secret_sha256(SESSION_TOKEN), "label": "Firefox"}
    body.update(over)
    return json.dumps(body).encode()


def _signin(app, body=None, origin=ORIGIN, ctype=JSON):
    headers = [ctype] if ctype else []
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    status, out = call(app, "POST", "/api/console/session", headers=headers,
                       body=_signin_body() if body is None else body)
    return status, out


def test_a_code_signs_the_browser_in_and_its_token_works_at_once(snapshot, sessions, redeemed):
    app, _ = _app()
    status, out = _signin(app)
    assert status == 200
    assert json.loads(out)["principal"] == "default" and json.loads(out)["session_id"] == "new1"
    assert redeemed["calls"][0]["code_hash"] == secret_sha256("ABCDEFGHJKMN")
    assert redeemed["calls"][0]["origin"] == "http://localhost:8765"
    assert redeemed["calls"][0]["valid"] is True
    status, _ = call(app, "GET", "/api/stats", headers=_auth(SESSION_TOKEN))
    assert status == 200


def test_the_origin_must_be_a_browsers(snapshot, sessions, redeemed):
    app, _ = _app()
    assert _signin(app, origin=None)[0] == 403
    assert _signin(app, origin="null")[0] == 403
    assert redeemed["calls"] == []


def test_the_origin_is_compared_in_its_normal_form(snapshot, sessions, redeemed):
    app, _ = _app()
    assert _signin(app, origin="http://127.0.0.1:8765/")[0] == 200
    assert _signin(app, origin="http://[::1]:8765")[0] == 200
    assert _signin(app, origin="https://bank.example.com")[0] == 200
    assert [c["origin"] for c in redeemed["calls"]] == [
        "http://localhost:8765", "http://localhost:8765", "https://bank.example.com"]


def test_only_a_json_body_is_read(snapshot, sessions, redeemed):
    app, _ = _app()
    assert _signin(app, ctype=(b"content-type", b"text/plain"))[0] == 415
    assert redeemed["calls"] == []


@pytest.mark.parametrize("body", [
    _signin_body(code="not-a-code"), _signin_body(token_sha256="AB" * 32),
    _signin_body(label=7), _signin_body(label="x" * 65), _signin_body(extra=1),
    json.dumps({"code": CODE, "token_sha256": secret_sha256(SESSION_TOKEN)}).encode(),
    b"[]", b"not json", b"",
], ids=lambda b: repr(b)[:30])
def test_every_malformed_sign_in_gets_the_same_refusal(snapshot, sessions, redeemed, body):
    app, _ = _app()
    assert _signin(app, body=body) == (400, b'{"error": "signin_refused"}')
    assert redeemed["calls"] == []


def test_a_refused_code_gets_the_same_refusal(snapshot, sessions, redeemed):
    redeemed["result"] = None
    app, _ = _app()
    assert _signin(app) == (400, b'{"error": "signin_refused"}')


def test_a_large_body_is_refused(snapshot, sessions, redeemed):
    app, _ = _app()
    assert _signin(app, body=_signin_body(label="x" * 2000))[0] == 413


def test_bad_codes_are_held_at_twenty_a_minute(snapshot, sessions, redeemed):
    redeemed["result"] = None
    app, _ = _app()
    for _ in range(20):
        assert _signin(app)[0] == 400
    assert _signin(app) == (429, b'{"error": "rate_limited"}')


def test_sign_in_has_its_own_budget_apart_from_pairing(snapshot, sessions, redeemed):
    app, _ = _app()
    for _ in range(25):
        call(app, "POST", "/api/pair", headers=[JSON], body=b"{}")
    assert _signin(app)[0] == 200


def test_an_open_daemon_refuses_sign_in(snapshot, sessions, redeemed):
    app, _ = _app(token=None)
    assert _signin(app) == (400, b'{"error": "signin_refused"}')
    assert redeemed["calls"] == []


def test_a_database_failure_is_unavailable_not_a_refusal(snapshot, sessions, monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(console_sessions, "redeem_code", boom)
    app, _ = _app()
    status, out = _signin(app)
    assert (status, json.loads(out)) == (503, {"error": "signin_unavailable"})


# -- GET /api/console/sessions and revoke ----------------------------------------

@pytest.fixture
def listed(monkeypatch):
    state = {"calls": [], "revoked": []}
    rows = [console_sessions.describe(_session(sid="s1")),
            console_sessions.describe(_session(STORE_SESSION_TOKEN, principal="laptop",
                                               source="store", sid="s2"))]

    def fake_list(dsn, *, principal=None):
        state["calls"].append(principal)
        return [r for r in rows if principal is None or r["principal"] == principal]

    def fake_revoke(dsn, session_id, *, principal=None):
        state["revoked"].append((session_id, principal))
        return any(r["session_id"] == session_id
                   and (principal is None or r["principal"] == principal) for r in rows)

    monkeypatch.setattr(console_sessions, "list_sessions", fake_list)
    monkeypatch.setattr(console_sessions, "revoke_session", fake_revoke)
    return state


def test_an_operator_lists_every_session_and_sees_which_is_its_own(snapshot, sessions, listed):
    sessions.add(_session())
    app, _ = _app()
    status, out = call(app, "GET", "/api/console/sessions", headers=_auth(SESSION_TOKEN))
    body = json.loads(out)
    assert status == 200 and body["operator"] is True and listed["calls"] == [None]
    assert [(s["session_id"], s["current"]) for s in body["sessions"]] == [
        ("s1", True), ("s2", False)]
    assert all("token_hash" not in s and "issuer" not in s for s in body["sessions"])


def test_a_paired_machine_lists_and_revokes_only_its_own(snapshot, sessions, listed):
    _store_session(sessions, snapshot)
    app, _ = _app()
    status, out = call(app, "GET", "/api/console/sessions", headers=_auth(STORED_TOKEN))
    body = json.loads(out)
    assert body["operator"] is False and [s["session_id"] for s in body["sessions"]] == ["s2"]
    status, out = call(app, "POST", "/api/console/sessions/revoke", headers=_auth(STORED_TOKEN),
                       body=json.dumps({"session_id": "s1"}).encode())
    assert json.loads(out) == {"revoked": False}
    assert listed["revoked"] == [("s1", "laptop")]


def test_revoking_a_session_signs_it_out_at_once(snapshot, sessions, listed):
    sessions.add(_session())
    app, _ = _app()
    status, out = call(app, "POST", "/api/console/sessions/revoke", headers=_auth(SESSION_TOKEN),
                       body=json.dumps({"session_id": "s1"}).encode())
    assert json.loads(out) == {"revoked": True}
    status, _ = call(app, "GET", "/api/stats", headers=_auth(SESSION_TOKEN))
    assert status == 401


def test_an_unknown_console_route_is_not_found(snapshot, sessions):
    app, _ = _app()
    status, _ = call(app, "GET", "/api/console/nope", headers=_auth(ENV_TOKEN))
    assert status == 404
    status, _ = call(app, "GET", "/api/console/link", headers=_auth(ENV_TOKEN))
    assert status == 405


# -- /health ------------------------------------------------------------------------

def test_health_says_which_sign_ins_the_daemon_offers(sessions):
    from pseudolife_memory.daemon import _build_health_payload
    service = FixtureService()
    service._db_url = "postgresql://fixture/unused"
    assert _build_health_payload(service, True)["console_signin"] == {"link": True,
                                                                      "passkey": False}
    assert _build_health_payload(service, False)["console_signin"]["link"] is False
    service._db_url = None
    assert _build_health_payload(service, True)["console_signin"]["link"] is False


# -- review follow-ups: the gate's other call sites, staleness, near misses -------

def test_a_session_reads_the_board_view_and_the_maintainer_status(snapshot, sessions):
    sessions.add(_session())
    app, _ = _app()
    for path, query in (("/api/agents", "view=coordination"), ("/api/maintainer/status", "")):
        status, out = call(app, "GET", path, headers=_auth(SESSION_TOKEN), query=query)
        assert status != 401, (path, out)
        assert b'"unauthorized"' not in out and b"authentication_required" not in out, (path, out)


def test_a_stale_session_view_is_503_through_the_gate(snapshot):
    previous = installed_sessions()
    snap = SessionSnapshot(max_age=60.0, clock=lambda: 0.0)
    snap.refresh(lambda: ([_session()], []))
    snap._clock = lambda: 1000.0
    install_sessions(snap)
    try:
        app, _ = _app()
        status, out = call(app, "GET", "/api/stats", headers=_auth(SESSION_TOKEN))
    finally:
        install_sessions(previous)
    assert (status, json.loads(out)) == (503, {"error": "principals_unavailable"})


def test_an_environment_session_resolves_while_the_stored_view_is_stale(sessions):
    previous = installed_store()
    store = PrincipalSnapshot(max_age=60.0, clock=lambda: 0.0)
    store.refresh(lambda: ([], None))
    store._clock = lambda: 1000.0
    install_store(store)
    try:
        sessions.add(_session())
        app, _ = _app()
        status, _ = call(app, "GET", "/api/stats", headers=_auth(SESSION_TOKEN))
    finally:
        install_store(previous)
    assert status == 200


def test_a_session_a_paired_machine_issued_cannot_issue_either(snapshot, sessions):
    _store_session(sessions, snapshot)
    app, _ = _app()
    status, out = call(app, "POST", "/api/console/link", headers=_auth(STORE_SESSION_TOKEN),
                       body=json.dumps({"origin": ORIGIN}).encode())
    assert status == 403 and json.loads(out)["error"] == "session_cannot_issue"


@pytest.mark.parametrize("path", ["/mcp/", "/mcp/messages", "/api/coordination/send"])
def test_near_miss_agent_paths_refuse_a_session(snapshot, sessions, path):
    sessions.add(_session())
    app, _ = _app()
    status, _ = call(app, "POST", path, headers=_auth(SESSION_TOKEN), body=b"{}")
    assert status == 401


def test_a_stale_stored_view_at_redemption_is_unavailable_not_refused(sessions, monkeypatch):
    previous = installed_store()
    store = PrincipalSnapshot(max_age=60.0, clock=lambda: 0.0)
    store.refresh(lambda: ([], None))
    store._clock = lambda: 1000.0
    install_store(store)

    def fake(dsn, code_hash, token_hash, *, origin, label, still_valid):
        still_valid("laptop", "store", secret_sha256(STORED_TOKEN))
        raise AssertionError("still_valid should have raised")

    monkeypatch.setattr(console_sessions, "redeem_code", fake)
    try:
        app, _ = _app()
        status, out = _signin(app)
    finally:
        install_store(previous)
    assert (status, json.loads(out)) == (503, {"error": "signin_unavailable"})


def test_the_list_leaves_out_sessions_that_no_longer_resolve(snapshot, sessions, listed):
    # s1's issuing environment token is gone (another token signed it in).
    sessions.add(_session(issuer=secret_sha256("a-removed-token")))
    _store_session(sessions, snapshot)
    app, _ = _app()
    status, out = call(app, "GET", "/api/console/sessions", headers=_auth(ENV_TOKEN))
    assert [s["session_id"] for s in json.loads(out)["sessions"]] == ["s2"]


def test_re_pairing_through_the_route_signs_the_old_tokens_session_out(snapshot, sessions,
                                                                      monkeypatch):
    _store_session(sessions, snapshot)
    new = StoredPrincipal("laptop", secret_sha256("a-new-paired-token"), None, True, False)
    monkeypatch.setattr("pseudolife_memory.principal_store.redeem",
                        lambda dsn, code_hash, token_hash, *, excluded=(): new)
    app, _ = _app()
    status, _ = call(app, "GET", "/api/stats", headers=_auth(STORE_SESSION_TOKEN))
    assert status == 200
    status, _ = call(app, "POST", "/api/pair", headers=[JSON], body=json.dumps(
        {"code": CODE, "token_sha256": secret_sha256("a-new-paired-token")}).encode())
    assert status == 200
    status, _ = call(app, "GET", "/api/stats", headers=_auth(STORE_SESSION_TOKEN))
    assert status == 401
