"""Focused review controls: exact HTTP bytes and explicitly named JSONB domains."""
import base64
import hashlib
import json
import os
import queue
import secrets
import threading
from pathlib import Path
import socket
import subprocess
from contextlib import closing
from types import SimpleNamespace

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from evals.rust_baseline.daemon import disposable_database, free_port
from evals.rust_port.test_sent_http_process import CONFIG, TOKEN, oracle, paired, prefix, seed  # noqa: F401
from evals.rust_port.sent_http import native_sent
from evals.rust_port.maintainer_sent import compare_response, observe_http
from evals.rust_port.harness import isolated_env, HttpClient
from evals.rust_port.processes import owned_process


MERGE_FIELDS = ["enabled: false", "allowed_principals: []", "enabled: false, allowed_principals: []"]


def merge_configuration(fields):
    return (f"defaults: &deny {{{fields}}}\ncoordination:\n  <<: *deny\n"
            "  maintainer:\n    rp_id: localhost\n    origin: http://localhost\n")


def startup_refusal(prefix, private, configuration):
    """Capture an expected pre-readiness refusal and verify owned cleanup."""
    private = Path(private)
    private.mkdir(parents=True, exist_ok=True)
    config = private / "config.yaml"
    config.write_text(configuration, encoding="utf-8")
    env = isolated_env(private / "home")
    env.update(PSEUDOLIFE_MCP_CONFIG=str(config), PSEUDOLIFE_MCP_HOST="127.0.0.1", PSEUDOLIFE_MCP_PORT="0")
    with owned_process([*prefix, "serve"], cwd=private, env=env, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        out, error = process.communicate(timeout=15)
        assert process.returncode == 1
        assert out == b""
    assert process.owned_cleanup == {"process_stopped": True, "subtree_stopped": True}
    return error


@pytest.mark.parametrize("fields", MERGE_FIELDS)
def test_implicit_yaml_merge_startup_refused(prefix, tmp_path, fields):
    error = startup_refusal(prefix, tmp_path, merge_configuration(fields))
    assert error == ("pseudolife-stdio serve: config-yaml-typed: implicit YAML merge key '<<' unsupported; quote the string (file: config.yaml)" + os.linesep).encode()


@pytest.mark.parametrize("quote", ["'", '"'])
def test_quoted_yaml_merge_key_and_value_http_bytes(prefix, tmp_path, monkeypatch, quote):
    text = (f"ignored: {quote}<<{quote}\ncoordination:\n  {quote}<<{quote}: {{enabled: false}}\n"
            "  maintainer:\n    rp_id: localhost\n    origin: http://localhost\n")
    paired(prefix, tmp_path, monkeypatch, configuration=text)


@pytest.mark.parametrize("fields", MERGE_FIELDS)
def test_ordinary_yaml_alias_admission_http_bytes(prefix, tmp_path, monkeypatch, fields):
    text = (f"defaults: &deny {{{fields}, maintainer: {{rp_id: localhost, origin: 'http://localhost'}}}}\n"
            "coordination: *deny\n")
    paired(prefix, tmp_path, monkeypatch, configuration=text)


def pg_open_observation(prefix, private, dsn, *, ambient=None, default_file=None):
    """Observe both snapshot and request opens through an owned TCP process."""
    private = Path(private)
    private.mkdir(parents=True, exist_ok=True)
    config = private / "config.yaml"
    config.write_text(json.dumps(CONFIG), encoding="utf-8")
    env = isolated_env(private / "home")
    port, nonce = free_port(), secrets.token_hex(32)
    env.update(PSEUDOLIFE_MCP_CONFIG=str(config), PSEUDOLIFE_MCP_DATABASE_URL=dsn,
               PSEUDOLIFE_MCP_HOST="127.0.0.1", PSEUDOLIFE_MCP_PORT=str(port),
               PSEUDOLIFE_MCP_TOKEN=TOKEN, PSEUDOLIFE_BASELINE_NONCE=nonce)
    env.update(ambient or {})
    if default_file is not None:
        directory = Path(env["APPDATA"]) / "postgresql" if os.name == "nt" else Path(env["HOME"]) / ".postgresql"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / default_file).write_text("synthetic unsupported control", encoding="utf-8")
    diagnostics = []
    with owned_process([*prefix, "serve"], cwd=private, env=env, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        def collect():
            try:
                for line in process.stderr:
                    diagnostics.append(line)
            except ValueError:
                pass  # The ownership fixture closes streams after terminating.
        reader = threading.Thread(target=collect, daemon=True)
        reader.start()
        lines = queue.Queue()
        threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
        notice = json.loads(lines.get(timeout=20))
        assert notice["candidate"] == "rust-maintainer-sent" and notice["ready"] is True
        assert notice["nonce"] == nonce and notice["port"] == port
        assert process.owns_runtime_pid(notice["pid"])
        actual = observe_http(HttpClient(f"http://127.0.0.1:{port}"), headers={"Authorization": "Bearer " + TOKEN})
    reader.join(timeout=5)
    assert not reader.is_alive()
    assert process.owned_cleanup == {"process_stopped": True, "subtree_stopped": True}
    return actual, b"".join(diagnostics), process.owned_cleanup


@pytest.mark.parametrize("control", ["PGSSLMODE", "PGTLSCONTROL", "PGPASSWORD"])
def test_ambient_pg_policy_keeps_private_503(prefix, tmp_path, control):
    actual, error, _ = pg_open_observation(prefix, tmp_path,
        "host=127.0.0.1 port=1 dbname=synthetic_refusal sslmode=disable",
        ambient={control: "synthetic-value-not-for-stderr"})
    assert_private_503(actual)
    assert error.splitlines() == [b"pseudolife-stdio serve: pg-dsn-explicit-tls"] * 2


@pytest.mark.parametrize("name", ["postgresql.crt", "postgresql.key", "root.crl"])
def test_ambient_pg_file_keeps_private_503(prefix, tmp_path, name):
    actual, error, _ = pg_open_observation(prefix, tmp_path,
        "host=127.0.0.1 port=1 dbname=synthetic_refusal sslmode=disable", default_file=name)
    assert_private_503(actual)
    assert error.splitlines() == [b"pseudolife-stdio serve: pg-dsn-explicit-tls"] * 2


def test_snapshot_and_request_root_certificate_policy_refusal(prefix, tmp_path):
    missing = tmp_path / "synthetic-missing-root.pem"
    dsn = make_conninfo(host="127.0.0.1", port=1, dbname="synthetic_refusal", sslmode="verify-full", sslrootcert=str(missing),
                        user="synthetic-user-not-for-stderr", password="synthetic-password-not-for-stderr")
    actual, error, _ = pg_open_observation(prefix, tmp_path, dsn)
    assert_private_503(actual)
    assert error.splitlines() == [b"pseudolife-stdio serve: pg-dsn-explicit-tls"] * 2


def assert_private_503(actual):
    assert actual["status"] == 503
    assert base64.b64decode(actual["body_b64"]) == b'{"error": "coordination_unavailable"}'


def test_network_unavailability_keeps_private_http_503(prefix, tmp_path):
    # An owned, bound but non-listening socket keeps this endpoint unavailable.
    with socket.socket() as endpoint:
        endpoint.bind(("127.0.0.1", 0))
        dsn = make_conninfo(host="127.0.0.1", port=endpoint.getsockname()[1], dbname="synthetic_unavailable", sslmode="disable")
        actual, error, _ = pg_open_observation(prefix, tmp_path, dsn)
    assert_private_503(actual)
    assert error == b""


def raw_bank_snapshot(dsn):
    """Hash database text without decoding exceptional JSONB through Python."""
    captured = {}
    with psycopg.connect(dsn, autocommit=True) as conn:
        for table in ("coordination_agents", "coordination_messages", "coordination_events", "principals"):
            captured[table] = conn.execute(sql.SQL("SELECT md5(t::text) FROM {} t ORDER BY t::text").format(sql.Identifier(table))).fetchall()
    return hashlib.sha256(json.dumps(captured, sort_keys=True).encode()).hexdigest()


def test_normalized_principal_http_bytes(prefix, tmp_path, monkeypatch):
    config = {"coordination": {"allowed_principals": [" DEFAULT ", " DeFaUlT "], **CONFIG["coordination"]}}
    paired(prefix, tmp_path, monkeypatch, config=config)


@pytest.mark.parametrize("name", ["1_agent", "123_name"])
def test_digit_name_http_bytes(prefix, tmp_path, monkeypatch, name):
    configuration = f"coordination:\n  allowed_principals: [{name}]\n  maintainer:\n    rp_id: localhost\n    origin: http://localhost\n"
    paired(prefix, tmp_path, monkeypatch, configuration=configuration, token=None, tokens="synthetic-name:" + name,
           requests=[{"headers": {"Authorization": "Bearer synthetic-name"}}])


@pytest.mark.parametrize("raw,native_status,oracle_status,exact", [
    ("[" * 200 + "0" + "]" * 200, 200, 200, True),
    ("[" * 2048 + "0" + "]" * 2048, 200, 503, False),
    ("[" * 2049 + "0" + "]" * 2049, 503, 503, True),
    ('{"integer":' + "9" * 4300 + "}", 200, 200, True),
    ('{"integer":-' + "9" * 4300 + "}", 200, 200, True),
    ('{"integer":' + "9" * 4301 + "}", 200, 503, False),
    ('{"integer":-' + "9" * 4301 + "}", 200, 503, False),
], ids=["depth200-exact", "depth2048-policy", "depth2049-refusal", "digits4300-positive", "digits4300-negative", "digits4301-positive-policy", "digits4301-negative-policy"])
def test_jsonb_read_domains(prefix, tmp_path, monkeypatch, raw, native_status, oracle_status, exact):
    from pseudolife_memory.utils.config import load_config
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", TOKEN)
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKENS", raising=False)
    path = tmp_path / "oracle.yaml"
    path.write_text(json.dumps(CONFIG), encoding="utf-8")
    request = {"headers": {"Authorization": "Bearer " + TOKEN}}
    captures = []
    for native in (False, True):
        with disposable_database() as generated:
            dsn = make_conninfo(generated, sslmode="disable")
            seed(dsn)
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("UPDATE coordination_messages SET wake=%s::jsonb WHERE message_id=%s", (raw, "message-00"))
            before = raw_bank_snapshot(dsn)
            if native:
                with native_sent(prefix, tmp_path / "native", dsn, configuration=json.dumps(CONFIG), token=TOKEN) as (client, _):
                    observed = observe_http(client, **request)
            else:
                observed = oracle(dsn, [request], load_config(path), TOKEN, {})[0]
            assert raw_bank_snapshot(dsn) == before
            captures.append(observed)
    wanted, actual = captures
    assert wanted["status"] == oracle_status
    assert actual["status"] == native_status
    if exact:
        assert compare_response(wanted, actual) == []
    else:
        # Direct SQL injection is a policy control, never exact-oracle evidence.
        assert wanted["status"] == 503
        assert raw.encode().replace(b":", b": ") in base64.b64decode(actual["body_b64"])


def test_canonical_send_ingress_rejects_4301_before_store(tmp_path):
    from pseudolife_memory.maintainer import MaintainerOps
    from pseudolife_memory.web.fixtures import FixtureService
    from pseudolife_memory.web.api import build_console_app
    from pseudolife_memory.utils.config import load_config
    from tests.asgi_helpers import call_with_headers, stub_mcp

    class Producer(MaintainerOps, FixtureService):
        pass

    with disposable_database() as generated:
        dsn = make_conninfo(generated, sslmode="disable")
        seed(dsn)
        path = tmp_path / "config.yaml"
        path.write_text(json.dumps(CONFIG), encoding="utf-8")
        service = Producer()
        service.config = load_config(path)
        service._storage = SimpleNamespace(dsn=dsn)
        service._db_url = dsn
        app = build_console_app(stub_mcp, TOKEN, lambda: {}, service)
        before = raw_bank_snapshot(dsn)
        with pytest.raises(ValueError):
            call_with_headers(app, "POST", "/api/maintainer/send",
                              headers=[(b"authorization", ("Bearer " + TOKEN).encode()), (b"content-type", b"application/json")],
                              body=b'{"payload":"synthetic","mac":"synthetic","assertion":{"integer":' + b"9" * 4301 + b"}}")
        assert raw_bank_snapshot(dsn) == before


@pytest.mark.parametrize("depth,accepted", [(200, True), (2048, False), (2049, False)])
def test_canonical_board_send_depth(tmp_path, depth, accepted):
    proof = 0
    for _ in range(depth - 1):
        proof = [proof]
    proof = {"label": "synthetic", "depth_control": proof}
    board_send_proof(proof, accepted, RecursionError)


@pytest.mark.parametrize("sign", [1, -1])
def test_canonical_board_send_integer4301(sign):
    board_send_proof({"label": "synthetic", "integer_control": sign * (10 ** 4300)}, False, ValueError)


def board_send_proof(proof, accepted, exception):
    from pseudolife_memory.storage.coordination import CoordinationConnection, CoordinationStore

    with disposable_database() as generated:
        dsn = make_conninfo(generated, sslmode="disable")
        seed(dsn)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value JSONB)")
        with closing(CoordinationConnection(dsn)) as connection:
            store = CoordinationStore(connection, clock=lambda: 1000.0)
            sender = store.register("maintainer", label="maintainer")
            recipient = store.register("default", label="synthetic")
            before = raw_bank_snapshot(dsn)
            def send():
                return store.send("maintainer", sender["agent_id"], sender["credential"], to=recipient["agent_id"],
                                  text="synthetic depth control", request_id="synthetic-depth", maintainer_proof=proof)
            if accepted:
                assert send()["message_id"]
                assert raw_bank_snapshot(dsn) != before
            else:
                with pytest.raises(exception):
                    send()
                assert raw_bank_snapshot(dsn) == before
