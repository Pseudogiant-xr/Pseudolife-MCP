"""The maintainer routes over HTTP, the Console's security headers, and the
config route's refusal (schema v54; specs 2026-10-02-maintainer-wake-design.md
and 2026-10-04-board-roles-passkey.md)."""
from __future__ import annotations

import json
import threading

import pytest

from pseudolife_memory.storage.maintainer import MaintainerError, challenge_bytes
from pseudolife_memory.web import config_io
from pseudolife_memory.web.api import CONSOLE_CSP, build_console_app
from pseudolife_memory.web.fixtures import FixtureService
from tests.asgi_helpers import call, call_with_headers, stub_mcp

BEARER = "maintainer-web-bearer"
AUTH = [(b"authorization", f"Bearer {BEARER}".encode())]
JSON = [(b"content-type", b"application/json")]
EXACT_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
             "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; "
             "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")

ROUTES = [
    ("GET", "/api/maintainer", "maintainer_status", None),
    ("POST", "/api/maintainer/challenge", "maintainer_challenge", {"purpose": "send"}),
    ("POST", "/api/maintainer/enrol", "maintainer_enrol", {"payload": "p"}),
    ("POST", "/api/maintainer/send", "maintainer_send", {"payload": "p"}),
    ("POST", "/api/maintainer/role", "maintainer_role", {"payload": "p"}),
    ("POST", "/api/maintainer/cancel", "maintainer_cancel", {"payload": "p"}),
    ("POST", "/api/maintainer/revoke", "maintainer_revoke", {"payload": "p"}),
    ("POST", "/api/maintainer/repudiate", "maintainer_repudiate", {"payload": "p"}),
    ("GET", "/api/maintainer/sent", "maintainer_sent", None),
    ("GET", "/api/maintainer/inbox", "maintainer_inbox", None),
]


class Recorder(FixtureService):
    """Records which service method each route called, with what."""

    def __init__(self, raise_with=None):
        super().__init__()
        self.calls = []
        self.raise_with = raise_with
        for _m, _p, name, _b in ROUTES:
            setattr(self, name, self._recorder(name))

    def _recorder(self, name):
        def method(*args):
            self.calls.append((name, args))
            if self.raise_with is not None:
                raise self.raise_with
            return {"called": name}
        return method


def _app(service, token=BEARER):
    return build_console_app(stub_mcp, token, lambda: {}, service)


def _hit(app, method, path, body=None, headers=AUTH, query=""):
    raw = b"" if body is None else json.dumps(body).encode()
    return call(app, method, path, headers=headers + (JSON if raw else []), body=raw,
                query=query)


# ── headers ────────────────────────────────────────────────────────────────

def test_the_csp_constant_is_the_addendums_exact_value():
    assert CONSOLE_CSP == EXACT_CSP


@pytest.mark.parametrize("path", ["/ui/", "/ui/index.html", "/ui/no-such-file.js", "/"])
def test_console_responses_carry_the_csp_and_frame_headers(path):
    status, headers, _ = call_with_headers(_app(FixtureService()), "GET", path)
    assert status in (200, 307, 404)
    assert headers[b"content-security-policy"] == EXACT_CSP.encode()
    assert headers[b"x-frame-options"] == b"DENY"
    assert headers[b"referrer-policy"] == b"no-referrer"


# ── the route contract ─────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path,name,body", ROUTES)
def test_each_route_calls_its_one_service_method(method, path, name, body):
    service = Recorder()
    status, out = _hit(_app(service), method, path, body, query="limit=7")
    assert status == 200 and json.loads(out) == {"called": name}
    [(called, args)] = service.calls
    assert called == name
    if name in ("maintainer_sent", "maintainer_inbox"):
        assert args == (7,)
    elif name == "maintainer_status":
        assert args == ()
    else:
        assert args == (body,)


@pytest.mark.parametrize("method,path,name,body", ROUTES)
def test_a_tokenless_daemon_refuses_every_maintainer_route(method, path, name, body):
    service = Recorder()
    status, out = _hit(_app(service, token=None), method, path, body, headers=[])
    assert (status, json.loads(out)) == (401, {"error": "authentication_required"})
    assert service.calls == []


def test_a_wrong_bearer_is_unauthorized():
    service = Recorder()
    status, _ = _hit(_app(service), "GET", "/api/maintainer",
                     headers=[(b"authorization", b"Bearer nope")])
    assert status == 401 and service.calls == []


@pytest.mark.parametrize("error,status,code", [
    (MaintainerError("maintainer_https_required"), 409, "maintainer_https_required"),
    (MaintainerError("maintainer_not_enrolled"), 409, "maintainer_not_enrolled"),
    (MaintainerError("challenge_expired"), 410, "challenge_expired"),
    (MaintainerError("challenge_spent"), 410, "challenge_spent"),
    (MaintainerError("assertion_invalid", check="client_data_origin"), 403, "assertion_invalid"),
    (MaintainerError("recipient_unknown"), 404, "recipient_unknown"),
    (MaintainerError("recipient_reserved"), 400, "recipient_reserved"),
    (MaintainerError("rate_capped"), 429, "rate_capped"),
    (ValueError("challenge_spent"), 410, "challenge_spent"),       # a fixture's plain refusal
    (ValueError("C:\\Users\\someone secret"), 400, "invalid_request"),
    (RuntimeError("password=hunter2 in a database error"), 503, "coordination_unavailable"),
])
def test_refusals_answer_their_status_and_never_echo(error, status, code):
    app = _app(Recorder(raise_with=error))
    got, out = _hit(app, "POST", "/api/maintainer/send", {"payload": "p"})
    assert (got, json.loads(out)) == (status, {"error": code})
    assert b"client_data_origin" not in out and b"hunter2" not in out and b"Users" not in out


# ── config ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("patch", [
    {"coordination.maintainer.rp_id": "evil.example"},
    {"coordination.maintainer.origin": "https://evil.example"},
    {"coordination.maintainer": {"rp_id": "evil.example"}},
    {"Coordination.Maintainer.maintainer_per_recipient_per_hour": 1000},
])
def test_the_config_route_cannot_set_the_maintainer_keys(tmp_path, monkeypatch, patch):
    monkeypatch.delenv("PSEUDOLIFE_MCP_CONFIG", raising=False)
    service = FixtureService()
    service.data_dir = tmp_path
    with pytest.raises(ValueError, match="^config_protected$"):
        config_io.write_config(service, patch)
    status, out = _hit(_app(service), "POST", "/api/config", {"patch": patch})
    assert (status, json.loads(out)) == (400, {"error": "config_protected"})
    assert not (tmp_path / "config.yaml").exists()


def test_the_config_file_carries_the_maintainer_keys():
    from pseudolife_memory.utils.config import CoordinationConfig
    cfg = CoordinationConfig(maintainer={"rp_id": "box.example", "origin": "https://box.example:8443",
                                         "maintainer_per_recipient_per_hour": 5})
    assert cfg.maintainer.configured and cfg.maintainer.maintainer_per_recipient_per_hour == 5
    assert not CoordinationConfig().maintainer.configured
    with pytest.raises(ValueError):
        CoordinationConfig(maintainer={"rp_id": 7})
    with pytest.raises(ValueError):
        CoordinationConfig(maintainer={"maintainer_per_recipient_per_hour": -1})


# ── end to end over HTTP ───────────────────────────────────────────────────

def test_a_signed_send_over_http_end_to_end(pg_url, pg_conn, monkeypatch):
    """Bootstrap, confirm, send and receive through the real ASGI app and
    the bench Postgres, as the Console would."""
    from pseudolife_memory import coordination
    from pseudolife_memory.memory.hlc import HybridLogicalClock
    from pseudolife_memory.storage.maintainer import MaintainerStore
    from pseudolife_memory.storage.postgres import PostgresStorage
    from pseudolife_memory.maintainer import MaintainerOps
    from tests.maintainer_authenticator import ORIGIN, RP_ID, SoftAuthenticator

    class Service(MaintainerOps, FixtureService):
        pass

    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", BEARER)
    service = Service()
    service.config.coordination.enabled = True
    service.config.coordination.allowed_principals = ["default"]
    service.config.coordination.maintainer.rp_id = RP_ID
    service.config.coordination.maintainer.origin = ORIGIN
    service._storage = PostgresStorage(pg_url)
    service._db_url = pg_url
    service._lock = threading.Lock()
    service._hlc = HybridLogicalClock()
    service._ensure_init = lambda: None
    service._coordination_ready = True
    app = _app(service)

    status, out = _hit(app, "GET", "/api/maintainer")
    assert status == 200 and json.loads(out)["reason"] == "maintainer_not_enrolled"
    with service._coordination_lock:
        host = MaintainerStore(coordination._mailbox(service), rp_id="", origin="")
    code = host.bootstrap_code()
    auth = SoftAuthenticator(-257)
    status, out = _hit(app, "POST", "/api/maintainer/challenge",
                       {"purpose": "enrol-bootstrap", "label": "laptop"})
    challenge = json.loads(out)
    assert status == 200 and challenge["publicKey"]["attestation"] == "none"
    status, out = _hit(app, "POST", "/api/maintainer/enrol", {
        "payload": challenge["payload"], "mac": challenge["mac"], "code": code,
        "attestation": auth.register(challenge_bytes(challenge["payload"]))})
    assert (status, json.loads(out)) == (200, {"credential_id": auth.id, "label": "laptop",
                                               "state": "pending"})
    host.confirm(auth.id[:8])

    with service._coordination_lock:
        board = coordination._store(service)
    agent = board.register("default", project="p", label="worker")
    status, out = _hit(app, "POST", "/api/maintainer/challenge",
                       {"purpose": "send", "to": agent["agent_id"], "text": "ship it"})
    challenge = json.loads(out)
    status, out = _hit(app, "POST", "/api/maintainer/send", {
        "payload": challenge["payload"], "mac": challenge["mac"],
        "assertion": auth.assertion(challenge_bytes(challenge["payload"]))})
    sent = json.loads(out)
    assert status == 200 and set(sent) == {"message_id", "wake"}
    with service._coordination_lock:
        (message,) = board.receive("default", agent["agent_id"], agent["credential"])["messages"]
    assert message["origin"] == "maintainer" and message["verified"]["label"] == "laptop"
    # The same body again is spent.
    status, out = _hit(app, "POST", "/api/maintainer/send", {
        "payload": challenge["payload"], "mac": challenge["mac"],
        "assertion": auth.assertion(challenge_bytes(challenge["payload"]))})
    assert (status, json.loads(out)) == (410, {"error": "challenge_spent"})
    status, out = _hit(app, "GET", "/api/maintainer/sent", query="limit=5")
    assert status == 200 and json.loads(out)["messages"][0]["message_id"] == sent["message_id"]

    # A bearer cannot smuggle the origin or a proof through the board's REST
    # send, and plain mail stays agent mail.
    other = board.register("default", project="p", label="peer")
    instance = [(b"x-pl-agent", other["agent_id"].encode()),
                (b"x-pl-agent-key", other["credential"].encode())]
    for smuggled in ({"origin": "maintainer"}, {"maintainer_proof": {"label": "x"}}):
        status, out = _hit(app, "POST", "/api/coordination/send",
                           {"to": agent["agent_id"], "text": "forged", "request_id": "r1",
                            **smuggled}, headers=AUTH + instance)
        assert (status, json.loads(out)["error"]) == (400, "unexpected_parameter")
    status, out = _hit(app, "POST", "/api/coordination/send",
                       {"to": "maintainer", "text": "hi", "request_id": "r2"},
                       headers=AUTH + instance)
    assert json.loads(out)["error"] == "recipient_reserved"
    with service._coordination_lock:
        mail = board.receive("default", agent["agent_id"], agent["credential"])["messages"]
    assert [m["origin"] for m in mail] == ["maintainer"]


from tests.pg_fixtures import pg_conn, pg_url  # noqa: E402,F401
