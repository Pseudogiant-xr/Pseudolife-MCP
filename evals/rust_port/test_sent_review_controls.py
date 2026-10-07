"""Focused review controls: exact HTTP bytes and explicitly named JSONB domains."""
import base64
import hashlib
import json
from contextlib import closing
from types import SimpleNamespace

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from evals.rust_baseline.daemon import disposable_database
from evals.rust_port.test_sent_http_process import CONFIG, TOKEN, oracle, paired, prefix, seed  # noqa: F401
from evals.rust_port.sent_http import native_sent
from evals.rust_port.maintainer_sent import compare_response, observe_http


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
def test_jsonb_read_domains(prefix, tmp_path, raw, native_status, oracle_status, exact):
    from pseudolife_memory.utils.config import load_config
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
    from pseudolife_memory.storage.coordination import CoordinationConnection, CoordinationStore

    proof = 0
    for _ in range(depth - 1):
        proof = [proof]
    proof = {"label": "synthetic", "depth_control": proof}
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
                with pytest.raises(RecursionError):
                    send()
                assert raw_bank_snapshot(dsn) == before
