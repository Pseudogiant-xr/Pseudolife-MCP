"""Authenticated bank context is stable, bounded, and safe for legacy binding."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import hmac
import json
from types import SimpleNamespace
import threading
import uuid

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
import pytest

from pseudolife_memory.coordination import dispatch
from pseudolife_memory.storage.coordination import CoordinationError, CoordinationStore
from pseudolife_memory.web.api import build_console_app
from tests.asgi_helpers import call, stub_mcp
from tests.pg_fixtures import pg_conn, pg_service, pg_url  # noqa: F401


class Storage:
    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def _txn(self):
        with self.conn.transaction():
            yield


def _headers(token):
    return [(b"authorization", ("Bearer " + token).encode()),
            (b"content-type", b"application/json")]


def _guard_service(*, enabled=True, allowed=()):
    def unexpected_init():
        raise AssertionError("request reached storage initialization")

    return SimpleNamespace(
        config=SimpleNamespace(coordination=SimpleNamespace(
            enabled=enabled, allowed_principals=list(allowed))),
        _ensure_init=unexpected_init,
        _lock=threading.Lock(),
        _storage=None,
    )


@pytest.mark.parametrize(("token", "service", "status", "payload"), [
    ("wrong", _guard_service(allowed=("agent-user",)), 401, {"error": "unauthorized"}),
    ("denied", _guard_service(allowed=("agent-user",)), 403,
     {"error": "principal_not_allowed"}),
    ("allowed", _guard_service(enabled=False), 200, {"enabled": False}),
])
def test_context_route_gates_before_initialization(token, service, status, payload):
    app = build_console_app(stub_mcp, None, lambda: {}, service, token_map={
        "allowed": "agent-user", "denied": "denied-user"})
    actual, body = call(app, "POST", "/api/coordination/context", body=b"{}",
                        headers=_headers(token))
    assert actual == status
    result = json.loads(body)
    if "error" in payload:
        assert result["error"] == payload["error"]
    else:
        assert result == payload


def test_context_uses_preconnected_storage_without_full_service_init(monkeypatch):
    from pseudolife_memory import coordination

    service = _guard_service(allowed=("default",))
    service._storage = object()

    class Store:
        def context(self, principal, **parameters):
            assert parameters == {}
            return {"bank_id": "11111111-1111-4111-8111-111111111111",
                    "principal": principal}

    monkeypatch.setattr(coordination, "_store", lambda unused: Store())
    result = dispatch(service, "context", {}, principal="default", headers={})
    assert result["principal"] == "default"


def test_context_route_needs_no_instance_headers_and_persists_identity(pg_service):
    pg_service.config.coordination.enabled = True
    pg_service.config.coordination.allowed_principals = ["agent-user"]
    app = build_console_app(stub_mcp, None, lambda: {}, pg_service,
                            token_map={"fixture-secret": "agent-user"})

    status, body = call(app, "POST", "/api/coordination/context", body=b"{}",
                        headers=_headers("fixture-secret"))

    assert status == 200
    result = json.loads(body)
    assert set(result) == {"bank_id", "principal"}
    assert result["principal"] == "agent-user"
    assert str(uuid.UUID(result["bank_id"])) == result["bank_id"]
    assert pg_service._storage.conn.execute(
        "SELECT value FROM meta WHERE key='coordination_bank_id'").fetchone() == (
            result["bank_id"],)

    store = CoordinationStore(pg_service._storage)
    agent = store.register("agent-user")
    nonce = "0123456789abcdef0123456789abcdef"
    status, body = call(
        app, "POST", "/api/coordination/context",
        body=json.dumps({"agent_id": agent["agent_id"], "nonce": nonce}).encode(),
        headers=_headers("fixture-secret"))
    assert status == 200
    proved = json.loads(body)
    key = hashlib.sha256(agent["credential"].encode()).digest()
    message = json.dumps([
        "pseudolife-context-v1", result["bank_id"], "agent-user",
        agent["agent_id"], nonce,
    ], separators=(",", ":"), ensure_ascii=True).encode("ascii")
    assert proved == {**result, "proof": hmac.new(
        key, message, hashlib.sha256).hexdigest()}


def test_context_route_sanitizes_malformed_stored_identity(pg_service, caplog):
    pg_service.config.coordination.enabled = True
    pg_service.config.coordination.allowed_principals = ["agent-user"]
    stored = "private-invalid-bank-marker"
    pg_service._storage.conn.execute(
        "INSERT INTO meta (key,value) VALUES (%s,%s) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
        ("coordination_bank_id", Jsonb(stored)))
    app = build_console_app(stub_mcp, None, lambda: {}, pg_service,
                            token_map={"fixture-secret": "agent-user"})

    status, body = call(app, "POST", "/api/coordination/context", body=b"{}",
                        headers=_headers("fixture-secret"))

    assert status == 500
    assert json.loads(body) == {"error": "invalid_bank_identity"}
    assert stored not in body.decode() + caplog.text
    assert pg_service._storage.conn.execute(
        "SELECT value FROM meta WHERE key='coordination_bank_id'").fetchone() == (stored,)


@pytest.mark.parametrize("binding_headers", [
    [(b"x-pl-bank", b"22222222-2222-4222-8222-222222222222"),
     (b"x-pl-principal", b"agent-user")],
    [(b"x-pl-bank", b"11111111-1111-4111-8111-111111111111")],
])
def test_wrong_or_partial_bound_context_cannot_register(pg_service, binding_headers):
    pg_service.config.coordination.enabled = True
    pg_service.config.coordination.allowed_principals = ["agent-user"]
    store = CoordinationStore(pg_service._storage)
    bank_id = store.context("agent-user")["bank_id"]
    binding_headers = [
        (key, bank_id.encode() if value.startswith(b"11111111") else value)
        for key, value in binding_headers]
    app = build_console_app(stub_mcp, None, lambda: {}, pg_service,
                            token_map={"fixture-secret": "agent-user"})
    before = pg_service._storage.conn.execute(
        "SELECT count(*) FROM coordination_agents").fetchone()[0]

    status, body = call(
        app, "POST", "/api/coordination/register", body=b"{}",
        headers=_headers("fixture-secret") + binding_headers)

    assert status == 409
    assert json.loads(body) == {"error": "bank_identity_mismatch"}
    assert pg_service._storage.conn.execute(
        "SELECT count(*) FROM coordination_agents").fetchone()[0] == before


@pytest.mark.parametrize("principal", ["agent-user", "r\u00e9viseur", "\u5ba1\u9605\u8005", "review%41"])
def test_matching_bound_context_allows_register_and_context_ignores_binding(pg_service, principal):
    from urllib.parse import quote

    pg_service.config.coordination.enabled = True
    pg_service.config.coordination.allowed_principals = [principal]
    app = build_console_app(stub_mcp, None, lambda: {}, pg_service,
                            token_map={"fixture-secret": principal})
    status, body = call(app, "POST", "/api/coordination/context", body=b"{}",
                        headers=_headers("fixture-secret"))
    assert status == 200
    context = json.loads(body)
    bound = [(b"x-pl-bank", context["bank_id"].encode()),
             (b"x-pl-principal", quote(principal, safe="").encode("ascii"))]

    status, body = call(app, "POST", "/api/coordination/register", body=b"{}",
                        headers=_headers("fixture-secret") + bound)
    assert status == 200 and json.loads(body)["principal"] == principal
    status, body = call(
        app, "POST", "/api/coordination/context", body=b"{}",
        headers=_headers("fixture-secret") + [
            (b"x-pl-bank", b"wrong"), (b"x-pl-principal", b"wrong")])
    assert status == 200 and json.loads(body) == context


@pytest.mark.parametrize("encoded", ["%", "%FF", "%c3%a9", "%61gent-user", "%00", "raw\u00e9"])
def test_binding_rejects_invalid_or_noncanonical_principal_encoding(encoded):
    from pseudolife_memory.coordination import bound_identity

    with pytest.raises(ValueError, match="bank_identity_mismatch"):
        bound_identity({"x-pl-bank": "11111111-1111-4111-8111-111111111111",
                        "x-pl-principal": encoded})


def test_mcp_bound_context_is_checked_before_forwarding(pg_service):
    pg_service.config.coordination.enabled = True
    pg_service.config.coordination.allowed_principals = ["agent-user"]
    calls = []

    async def mcp(scope, receive, send):
        calls.append(scope["path"])
        await stub_mcp(scope, receive, send)

    app = build_console_app(mcp, None, lambda: {}, pg_service,
                            token_map={"fixture-secret": "agent-user"})
    base = _headers("fixture-secret") + [(b"x-pl-principal", b"agent-user")]
    status, body = call(app, "POST", "/mcp", headers=base + [
        (b"x-pl-bank", b"22222222-2222-4222-8222-222222222222")])
    assert status == 409
    assert json.loads(body) == {"error": "bank_identity_mismatch"}
    assert calls == []

    bank_id = CoordinationStore(pg_service._storage).context("agent-user")["bank_id"]
    status, _ = call(app, "POST", "/mcp", headers=base + [
        (b"x-pl-bank", bank_id.encode())])
    assert status == 501
    assert calls == ["/mcp"]


def test_context_identity_is_stable_across_connections_and_concurrent_creation(pg_url, pg_conn):
    pg_conn.autocommit = True
    pg_conn.execute("DELETE FROM meta WHERE key='coordination_bank_id'")
    barrier = threading.Barrier(2)

    def read_context(_):
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute("SET search_path TO public")
            barrier.wait(timeout=3)
            return CoordinationStore(Storage(conn)).context("alice")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(read_context, range(2)))

    assert results[0] == results[1]
    assert str(uuid.UUID(results[0]["bank_id"])) == results[0]["bank_id"]
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("SET search_path TO public")
        assert CoordinationStore(Storage(conn)).context("alice") == results[0]


def test_independent_bank_differs_while_metadata_clone_keeps_identity(pg_url):
    names = ["context_" + uuid.uuid4().hex for _ in range(3)]
    try:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            for name in names:
                conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
                conn.execute(sql.SQL(
                    "CREATE TABLE {}.meta (key TEXT PRIMARY KEY, value JSONB NOT NULL)"
                ).format(sql.Identifier(name)))

        def context(schema):
            with psycopg.connect(pg_url, autocommit=True) as conn:
                conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                return CoordinationStore(Storage(conn)).context("alice")

        source = context(names[0])
        independent = context(names[1])
        assert independent["bank_id"] != source["bank_id"]
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute(sql.SQL(
                "INSERT INTO {}.meta (key,value) VALUES (%s,%s)"
            ).format(sql.Identifier(names[2])),
                         ("coordination_bank_id", Jsonb(source["bank_id"])))
        assert context(names[2]) == source
    finally:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            for name in names:
                conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(name)))


def test_context_creation_rolls_back_with_outer_transaction(pg_conn):
    pg_conn.autocommit = True
    pg_conn.execute("DELETE FROM meta WHERE key='coordination_bank_id'")
    store = CoordinationStore(Storage(pg_conn))

    with pytest.raises(RuntimeError, match="rollback"):
        with store.storage._txn():
            created = store.context("alice")
            raise RuntimeError("rollback")

    assert pg_conn.execute(
        "SELECT value FROM meta WHERE key='coordination_bank_id'").fetchone() is None
    assert store.context("alice")["bank_id"] != created["bank_id"]


@pytest.mark.parametrize("stored", ["not-a-uuid", {"uuid": "wrong-shape"}, None])
def test_malformed_stored_bank_identity_fails_closed_without_replacement(pg_conn, stored):
    pg_conn.autocommit = True
    pg_conn.execute("DELETE FROM meta WHERE key='coordination_bank_id'")
    pg_conn.execute("INSERT INTO meta (key,value) VALUES (%s,%s)",
                    ("coordination_bank_id", Jsonb(stored)))
    store = CoordinationStore(Storage(pg_conn))

    with pytest.raises(CoordinationError, match="invalid_bank_identity"):
        store.context("alice")

    assert pg_conn.execute(
        "SELECT value FROM meta WHERE key='coordination_bank_id'").fetchone() == (stored,)


def test_context_proof_binds_bank_principal_agent_and_nonce(pg_conn):
    pg_conn.autocommit = True
    store = CoordinationStore(Storage(pg_conn))
    agent = store.register("alice")
    nonce = "0123456789abcdef0123456789abcdef"

    result = store.context("alice", agent_id=agent["agent_id"], nonce=nonce)

    key = hashlib.sha256(agent["credential"].encode()).digest()
    message = json.dumps([
        "pseudolife-context-v1", result["bank_id"], "alice",
        agent["agent_id"], nonce,
    ], separators=(",", ":"), ensure_ascii=True).encode("ascii")
    assert hmac.compare_digest(result["proof"], hmac.new(key, message, hashlib.sha256).hexdigest())
    for index, replacement in enumerate((
            str(uuid.uuid4()), "bob", "f" * 32, "f" * 32), start=1):
        fields = ["pseudolife-context-v1", result["bank_id"], "alice",
                  agent["agent_id"], nonce]
        fields[index] = replacement
        changed = json.dumps(fields, separators=(",", ":"),
                             ensure_ascii=True).encode("ascii")
        assert not hmac.compare_digest(
            result["proof"], hmac.new(key, changed, hashlib.sha256).hexdigest())


@pytest.mark.parametrize("mutation", ["wrong_principal", "revoked", "malformed_hash"])
def test_context_proof_refuses_unverifiable_mailbox(pg_conn, mutation):
    pg_conn.autocommit = True
    store = CoordinationStore(Storage(pg_conn))
    agent = store.register("alice")
    if mutation == "revoked":
        pg_conn.execute("UPDATE coordination_agents SET credential_hash=NULL WHERE agent_id=%s",
                        (agent["agent_id"],))
    elif mutation == "malformed_hash":
        pg_conn.execute("UPDATE coordination_agents SET credential_hash='bad' WHERE agent_id=%s",
                        (agent["agent_id"],))
    principal = "bob" if mutation == "wrong_principal" else "alice"

    with pytest.raises(CoordinationError, match="invalid_credential"):
        store.context(principal, agent_id=agent["agent_id"], nonce="0" * 32)


@pytest.mark.parametrize(("parameters", "code"), [
    ({"agent_id": "0" * 32}, "missing_parameter"),
    ({"nonce": "0" * 32}, "missing_parameter"),
    ({"agent_id": "0" * 31, "nonce": "0" * 32}, "invalid_agent_id"),
    ({"agent_id": "0" * 32, "nonce": "g" * 32}, "invalid_nonce"),
    ({"agent_id": "0" * 32, "nonce": "0" * 32, "credential": "secret"},
     "unexpected_parameter"),
])
def test_context_rejects_partial_malformed_or_secret_parameters(monkeypatch, parameters, code):
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "fixture-secret")
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKENS", raising=False)
    service = _guard_service(allowed=("default",))
    with pytest.raises(ValueError, match=code):
        dispatch(service, "context", parameters,
                 headers={"authorization": "Bearer fixture-secret"})
