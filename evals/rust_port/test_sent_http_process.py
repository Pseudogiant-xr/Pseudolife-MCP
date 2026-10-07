"""Whole Python HTTP/SQL oracle versus independent native HTTP bytes.

Each arm owns a separate generated initialized bank, sequentially. This fixture
uses the actual MaintainerOps gate/store and Python ASGI route, with a warm SQL
storage sentinel; no embedder, daemon writer lease or cold-bank migration runs.
"""
import base64
import hashlib
import json
import threading
from types import SimpleNamespace

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
import pytest

from evals.rust_baseline.daemon import disposable_database
from evals.rust_port.maintainer_sent import OWNED_HEADERS, compare_response, observe_http
from evals.rust_port.sent_http import native_sent

TOKEN = "synthetic-sent-http"
CONFIG = {"coordination": {"maintainer": {"rp_id": "localhost", "origin": "http://localhost"}}}


def seed(dsn, *, proof="falsy-corpus", missing=False, principals=()):
    from pseudolife_memory.storage.schema import COORDINATION_SCHEMA_SQL, PRINCIPALS_SCHEMA_SQL
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(PRINCIPALS_SCHEMA_SQL)
        for name, token, board, revoked in principals:
            conn.execute("INSERT INTO principals(principal,token_hash,board,revoked_at,created_at) VALUES(%s,%s,%s,%s,%s)",
                         (name, hashlib.sha256(token.encode()).hexdigest(), board, 1.0 if revoked else None, 1.0))
        if missing:
            return
        conn.execute(COORDINATION_SCHEMA_SQL)
        for aid, principal, label in [("sender", "maintainer", ""), ("recipient", "default", 'é雪😀 " \\')]:
            conn.execute("INSERT INTO coordination_agents(agent_id,principal,label,created_at,last_activity) VALUES(%s,%s,%s,%s,%s)",
                         (aid, principal, label, 1.0, 1.0))
        proofs = [None, False, 0, "", {}, [], {"other": 1}, {"label": None}, {"label": {"bb": [2, 1], "a": "Ω"}}] if proof == "falsy-corpus" else [proof]
        for seq, item in enumerate(proofs):
            # Tied timestamps deliberately exercise ordered IDs; rows include
            # expired, read, acknowledged and repudiated states without filtering.
            conn.execute("INSERT INTO coordination_messages(message_id,sender_agent_id,recipient_agent_id,sender_principal,request_id,fingerprint,recipient_sequence,created_at,expires_at,origin,text,maintainer_proof,wake,first_read_at,acknowledged_at,repudiated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (f"message-{seq:02}", "sender", "recipient", "maintainer", f"request-{seq}", "fixture", seq, 1e16, 1.0, "maintainer", None if seq == 1 else 'Quote " slash \\ é雪😀\n\t', None if item is None else Jsonb(item), Jsonb({"long-key": 1e16, "bb": [2, 1], "a": "é", "integer": 10**60}), None if seq % 2 else -0.0, 1e-5 if seq % 2 else None, 1.0 if seq == 2 else None))
        # The selected SQL must exclude both the wrong origin and principal.
        for seq, origin, principal in [(1000, "agent", "maintainer"), (1001, "maintainer", "excluded")]:
            conn.execute("INSERT INTO coordination_messages(message_id,sender_agent_id,recipient_agent_id,sender_principal,request_id,fingerprint,recipient_sequence,created_at,expires_at,origin) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (f"excluded-{seq}", "sender", "recipient", principal, f"request-{seq}", "fixture", seq, 1e17, 1e18, origin))


def oracle(dsn, requests, configuration, token, tokens):
    from pseudolife_memory.maintainer import MaintainerOps
    from pseudolife_memory.web.fixtures import FixtureService
    from pseudolife_memory.web.api import build_console_app
    from pseudolife_memory.storage.coordination import CoordinationConnection
    from pseudolife_memory import principals
    from pseudolife_memory.principal_store import PrincipalSnapshot, load_rows
    from tests.asgi_helpers import call_with_headers, stub_mcp

    class SqlOracle(MaintainerOps, FixtureService):
        pass

    service = SqlOracle()
    service.config = configuration
    service._db_url = dsn
    service._storage = SimpleNamespace(dsn=dsn)  # already-initialized SQL tier
    service._coordination_storage = CoordinationConnection(dsn)
    service._coordination_lock = threading.Lock()
    snapshot = PrincipalSnapshot(shadowed=tokens.values())
    with psycopg.connect(dsn, autocommit=True) as conn:
        snapshot.refresh(lambda: load_rows(conn))
    previous = principals.installed_store()
    principals.install_store(snapshot)
    try:
        app = build_console_app(stub_mcp, token, lambda: {}, service, token_map=tokens)
        captured = []
        for request in requests:
            headers = [(key.encode(), value.encode("latin-1")) for key, value in request.get("headers", {}).items()]
            status, selected, raw = call_with_headers(app, request.get("method", "GET"), "/api/maintainer/sent", headers=headers, query=request.get("query", ""), body=request.get("body", b""))
            captured.append({"status": status, "headers": {name: selected[name.encode()].decode() for name in OWNED_HEADERS if name.encode() in selected}, "body_b64": base64.b64encode(raw).decode()})
        return captured
    finally:
        principals.install_store(previous)
        service._coordination_storage.close()


def bank_snapshot(dsn):
    """Inspect fixture state only between arms, when no daemon owns the bank."""
    from psycopg import sql
    tables = ("coordination_agents", "coordination_messages", "coordination_events", "principals")
    captured = {}
    with psycopg.connect(dsn, autocommit=True) as conn:
        for table in tables:
            if conn.execute("SELECT to_regclass(%s)", ("public." + table,)).fetchone()[0] is None:
                continue
            captured[table] = conn.execute(sql.SQL("SELECT to_jsonb(t) FROM {} t ORDER BY to_jsonb(t)::text").format(sql.Identifier(table))).fetchall()
    return hashlib.sha256(json.dumps(captured, default=str, sort_keys=True).encode()).hexdigest()


@pytest.fixture
def prefix(request):
    selected = getattr(request.config, "_port_sent_prefix", None)
    if selected is None:
        pytest.skip("explicit native sent executable required")
    return selected


def paired(prefix, tmp_path, monkeypatch, *, requests=None, config=None, configuration=None, token=TOKEN, tokens=None, proof="falsy-corpus", missing=False, stored=()):
    from pseudolife_memory.utils.config import load_config
    from pseudolife_memory.principals import parse_token_map
    config = CONFIG if config is None else config
    text = json.dumps(config) if configuration is None else configuration
    path = tmp_path / "oracle-config.yaml"
    path.write_text(text, encoding="utf-8")
    cfg = load_config(path)
    if token is None:
        monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN", raising=False)
    else:
        monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", token)
    if tokens is None:
        monkeypatch.delenv("PSEUDOLIFE_MCP_TOKENS", raising=False)
    else:
        monkeypatch.setenv("PSEUDOLIFE_MCP_TOKENS", tokens)
    requests = requests or [{"headers": {"Authorization": "Bearer " + TOKEN}}]
    with disposable_database() as generated:
        dsn = make_conninfo(generated, sslmode="disable")
        seed(dsn, proof=proof, missing=missing, principals=stored)
        before = bank_snapshot(dsn)
        expected = oracle(dsn, requests, cfg, token, parse_token_map(tokens))
        assert bank_snapshot(dsn) == before
    # The Python bank and every SQL connection are gone before the native arm.
    with disposable_database() as generated:
        dsn = make_conninfo(generated, sslmode="disable")
        seed(dsn, proof=proof, missing=missing, principals=stored)
        before = bank_snapshot(dsn)
        with native_sent(prefix, tmp_path / "native", dsn, configuration=text, token=token, tokens=tokens) as (client, process):
            actual = [observe_http(client, **request) for request in requests]
        assert bank_snapshot(dsn) == before
    for request, wanted, observed in zip(requests, expected, actual, strict=True):
        assert compare_response(wanted, observed) == [], request.get("query", "admission")
    return {"expected": expected, "actual": actual, "differences": [],
            "readiness": process.sent_readiness, "owned_cleanup": process.owned_cleanup,
            "oracle_bank_unchanged": True, "native_bank_unchanged": True,
            "disposable_banks_dropped": True}


def test_whole_http_sql_order_and_query_controls(prefix, tmp_path, monkeypatch):
    queries = ["", "limit=1", "limit=0", "limit=-2", "limit=201", "limit=bad", "limit=1&limit=3", "limit=1_0", "limit=%2B2", "limit=%D9%A2", "limit=" + "9"*4301]
    paired(prefix, tmp_path, monkeypatch, requests=[{"query": query, "headers": {"Authorization": "Bearer " + TOKEN}} for query in queries])


@pytest.mark.parametrize("proof", [[1], "nonempty", 1, True])
def test_truthy_nonobject_proof_is_private_503(prefix, tmp_path, monkeypatch, proof):
    paired(prefix, tmp_path, monkeypatch, proof=proof)


@pytest.mark.parametrize("config", [{}, {"coordination": {"enabled": False}}, {"coordination": {"allowed_principals": []}}, {"coordination": {"maintainer": {"rp_id": "localhost", "origin": "http://localhost:80"}}}])
def test_service_gate_bytes(prefix, tmp_path, monkeypatch, config):
    paired(prefix, tmp_path, monkeypatch, config=config)


def test_missing_coordination_schema_is_private_503(prefix, tmp_path, monkeypatch):
    paired(prefix, tmp_path, monkeypatch, missing=True)


def test_http_gate_order_and_body_refusals(prefix, tmp_path, monkeypatch):
    auth = {"Authorization": "Bearer " + TOKEN}
    requests = [{}, {"headers": {"Authorization": "Bearer wrong"}}, {"method": "DELETE", "headers": auth}, {"method": "POST", "headers": auth}, {"method": "POST", "headers": {**auth, "Content-Type": "text/plain"}, "body": b"{}"}, {"method": "POST", "headers": {**auth, "Content-Type": "application/json"}, "body": b"{"}, {"method": "POST", "headers": {**auth, "Content-Type": "application/json"}, "body": b"[]"}, {"method": "POST", "headers": {**auth, "Content-Type": "application/json"}, "body": b"x"*(256*1024+1)}]
    paired(prefix, tmp_path, monkeypatch, requests=requests)


def test_tokenless_origin_and_authentication_gate(prefix, tmp_path, monkeypatch):
    paired(prefix, tmp_path, monkeypatch, token=None, requests=[{}, {"headers": {"Origin": "https://example.test"}}, {"headers": {"Host": "example.test"}}])


def test_stored_snapshot_authentication_and_board_admission(prefix, tmp_path, monkeypatch):
    rows = [("stored", "stored-token", True, False), ("denied", "denied-token", False, False), ("revoked", "revoked-token", True, True)]
    paired(prefix, tmp_path, monkeypatch, stored=rows, requests=[{"headers": {"Authorization": "Bearer " + token}} for _, token, _, _ in rows])


def test_installer_yaml_and_deferred_constant(prefix, tmp_path):
    import urllib.request
    import urllib.error
    configuration = "embedding:\n  device: cpu\n  backend: torch\n  cpu_dtype: fp32\nmemory:\n  dream:\n    enabled: false\nupdates:\n  check_releases: false\ncoordination:\n  allowed_principals: ['yes', \"on\"]\n  maintainer:\n    rp_id: localhost\n    origin: http://localhost\n"
    with native_sent(prefix, tmp_path, "", configuration=configuration) as (client, _):
        assert observe_http(client)["status"] == 401
        observed = []
        for path in ("/health", "/api/maintainer", "/mcp", "/not-implemented"):
            try:
                response = client.opener.open(client.base_url + path, timeout=client.timeout)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                assert response.status == 501
                observed.append(response.read())
        assert len(set(observed)) == 1
        assert observed[0] == b'{"error": "route_deferred", "candidate": "rust-maintainer-sent", "deferred_route": "all paths other than /api/maintainer/sent"}'


@pytest.mark.parametrize("configuration", [
    "coordination:\n  enabled: yes", "coordination:\n  enabled: no",
    "coordination:\n  enabled: on", "coordination:\n  enabled: off",
    "ignored: 012", "ignored: 0o12", "ignored: 1:20", "ignored: 1_000",
    "ignored: 2026-1-2T3:04:05",
    "ignored: !custom value", "x: 1\nx: 2", "coordination:\n  enabled: 'true'",
    "coordination:\n  allowed_principals: [1]", "coordination:\n  maintainer:\n    rp_id: 1",
])
def test_named_yaml_startup_refusals(prefix, tmp_path, configuration):
    import subprocess
    from evals.rust_port.harness import isolated_env
    from evals.rust_port.processes import owned_process
    config = tmp_path / "config.yaml"
    config.write_text(configuration, encoding="utf-8")
    env = isolated_env(tmp_path / "home")
    env["PSEUDOLIFE_MCP_CONFIG"] = str(config)
    with owned_process([*prefix, "serve"], cwd=tmp_path, env=env,
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE) as process:
        out, error = process.communicate(timeout=10)
        assert process.returncode == 1
        assert not out
        assert b"config-yaml-typed:" in error


@pytest.mark.parametrize("origin", ["http://localhost:bad", "http://[::1]", "http://[bad]", "HTTP://localhost", "http://localhost:80?query"])
def test_origin_problem_priority(prefix, tmp_path, monkeypatch, origin):
    paired(prefix, tmp_path, monkeypatch, config={"coordination": {"maintainer": {"rp_id": "localhost", "origin": origin}}})
