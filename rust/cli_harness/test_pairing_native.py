"""Native pairing against an isolated test PostgreSQL and loopback daemons."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from .fixture_daemon import FixtureDaemon, json_body, text
from .rows import _bank


@pytest.fixture
def candidate():
    default = Path(__file__).resolve().parents[1] / "target" / "release" / ("pseudolife-stdio.exe" if os.name == "nt" else "pseudolife-stdio")
    value = os.environ.get("PSEUDOLIFE_RUST_BINARY") or str(default)
    if not Path(value).is_file():
        pytest.skip("build the native client or set PSEUDOLIFE_RUST_BINARY")
    return str(Path(value).resolve(strict=True))


@pytest.fixture
def bank():
    name = _bank.name("pl_cf_pairing_native")
    _bank.create(name, schema=False)
    try:
        with _bank.connect(name) as conn:
            conn.execute("CREATE TABLE meta (key text PRIMARY KEY, value jsonb)")
            conn.execute("INSERT INTO meta VALUES ('coordination_bank_id', '\"pairing-native-fixture\"')")
            conn.execute("""CREATE TABLE principals (
                principal text PRIMARY KEY, token_hash text, tier text, board boolean,
                code_hash text, code_expires_at double precision, paired_code_hash text,
                created_at double precision, paired_at double precision,
                revoked_at double precision, owned boolean DEFAULT true)""")
        yield name
    finally:
        _bank.drop(name)


def environment(home):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PSEUDOLIFE_", "PG", "PL_"))}
    env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "appdata"),
               LOCALAPPDATA=str(home / "local"), XDG_CONFIG_HOME=str(home / "config"),
               PATH=str(home / "no-tools"), ProgramW6432=str(home / "no-programs"),
               PSEUDOLIFE_MCP_DATA_DIR=str(home / "no-bank"))
    return env


def invoke(candidate, home, bank, args, port=None):
    env = environment(home)
    env["PSEUDOLIFE_MCP_DATABASE_URL"] = _bank.url(bank)
    argv = [candidate, "invite", *args, "--json"]
    if port is not None:
        argv += ["--port", str(port)]
    return subprocess.run(argv, env=env, cwd=home, input=b"",
                          capture_output=True, timeout=20)


@pytest.mark.parametrize("suffix", ["/", "/./"])
def test_equivalent_absolute_home_is_native(candidate, tmp_path, suffix):
    env = environment(tmp_path)
    env["HOME"] = env["USERPROFILE"] = str(tmp_path) + suffix
    result = subprocess.run([candidate, "pair", "http://127.0.0.1:9", "invalid"],
                            env=env, cwd=tmp_path, input=b"", capture_output=True, timeout=20)
    reached_validation = b"not a pairing code" in result.stderr
    assert reached_validation, "absolute home spelling blocked valid path resolution"


def test_explicit_port_overrides_unused_default(candidate, tmp_path):
    env = environment(tmp_path)
    env["PSEUDOLIFE_MCP_PORT"] = "invalid-default"
    result = subprocess.run([candidate, "invite", "--list", "--port", "8765"],
                            env=env, cwd=tmp_path, input=b"", capture_output=True, timeout=20)
    reached_database = b"no database found" in result.stderr
    assert reached_database, "unused default port blocked an explicit valid port"


def assert_refusal(result, cause, code=4):
    import json
    # Never let pytest display a command, response or parsed report: an invite
    # that unexpectedly succeeds contains a newly minted pairing code.
    refused = result.returncode == code
    assert refused, "expected a native refusal"
    try:
        report = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        pytest.fail("refusal did not return a JSON report", pytrace=False)
    has_cause = cause in report.get("error", "")
    secret_absent = "fixture-private-secret" not in report.get("error", "")
    stderr_empty = not result.stderr
    assert has_cause, "refusal omitted the cause"
    assert secret_absent, "secret in diagnostic"
    assert stderr_empty, "JSON refusal also printed diagnostics"


@pytest.mark.parametrize("column,kind", [
    ("principal", "integer"), ("token_hash", "integer"), ("tier", "integer"),
    ("board", "text"), ("code_hash", "integer"), ("code_expires_at", "text"),
    ("paired_code_hash", "integer"), ("created_at", "text"),
    ("paired_at", "text"), ("revoked_at", "text"), ("owned", "text"),
])
def test_incompatible_invite_column_refuses_before_mutation(candidate, tmp_path, bank, column, kind):
    import psycopg.sql
    with _bank.connect(bank) as conn:
        conn.execute(psycopg.sql.SQL("ALTER TABLE principals ALTER COLUMN {} DROP DEFAULT").format(psycopg.sql.Identifier(column)))
        conn.execute(psycopg.sql.SQL("ALTER TABLE principals ALTER COLUMN {} TYPE {} USING NULL").format(
            psycopg.sql.Identifier(column), psycopg.sql.SQL(kind)))
    before = _bank.dump(bank)
    result = invoke(candidate, tmp_path, bank, ["laptop", "--url", "http://example.com"], port=9)
    assert_refusal(result, "column type")
    unchanged = before == _bank.dump(bank)
    assert unchanged, "refusal changed the bank"


@pytest.mark.parametrize("column,kind", [("key", "integer"), ("value", "text")])
def test_incompatible_meta_column_refuses_before_mutation(candidate, tmp_path, bank, column, kind):
    import psycopg.sql
    with _bank.connect(bank) as conn:
        conn.execute("DELETE FROM meta")
        conn.execute(psycopg.sql.SQL("ALTER TABLE meta ALTER COLUMN {} TYPE {} USING 1::{}").format(
            psycopg.sql.Identifier(column), psycopg.sql.SQL(kind), psycopg.sql.SQL(kind)))
    before = _bank.dump(bank)
    assert_refusal(invoke(candidate, tmp_path, bank, ["laptop"], port=9), "column type")
    unchanged = before == _bank.dump(bank)
    assert unchanged, "refusal changed the bank"


@pytest.mark.parametrize("epoch", ["NaN", "Infinity", "-1", "253402300800"])
def test_invalid_list_timestamp_refuses_without_partial_output(candidate, tmp_path, bank, epoch):
    with _bank.connect(bank) as conn:
        conn.execute("INSERT INTO principals (principal, board, created_at) VALUES ('laptop', true, %s)", (epoch,))
    before = _bank.dump(bank)
    assert_refusal(invoke(candidate, tmp_path, bank, ["--list"]), "timestamp")
    unchanged = before == _bank.dump(bank)
    assert unchanged, "listing changed the bank"
    with _bank.connect(bank) as conn:
        conn.execute("UPDATE principals SET created_at=1")
    succeeded = invoke(candidate, tmp_path, bank, ["--list"]).returncode == 0
    assert succeeded, "valid timestamp refused"


def test_null_invite_row_refuses_without_partial_output(candidate, tmp_path, bank):
    with _bank.connect(bank) as conn:
        conn.execute("INSERT INTO principals (principal, board, created_at) VALUES ('laptop', NULL, 1)")
    before = _bank.dump(bank)
    assert_refusal(invoke(candidate, tmp_path, bank, ["--list"]), "incompatible invite row")
    unchanged = before == _bank.dump(bank)
    assert unchanged
    with _bank.connect(bank) as conn:
        conn.execute("UPDATE principals SET board=true")
    succeeded = invoke(candidate, tmp_path, bank, ["--list"]).returncode == 0
    assert succeeded, "valid row refused"


@pytest.mark.parametrize("url", ["http://user:fixture-private-secret@example.com", "http://example.com/?fixture-private-secret", "http://example.com/#fixture-private-secret"], ids=["credentials", "query", "fragment"])
def test_invite_url_refuses_secret_bearing_shapes(candidate, tmp_path, bank, url):
    before = _bank.dump(bank)
    assert_refusal(invoke(candidate, tmp_path, bank, ["laptop", "--url", url], port=9), "--url", code=2)
    unchanged = before == _bank.dump(bank)
    assert unchanged, "URL refusal changed the bank"


def test_invalid_principal_diagnostic_does_not_echo_input(candidate, tmp_path, bank):
    result = invoke(candidate, tmp_path, bank, ["fixture-private-secret/invalid"])
    secret_absent = b"fixture-private-secret" not in result.stdout + result.stderr
    assert secret_absent, "principal error exposed the argument"


def test_valid_schema_still_creates_code_with_requested_grants(candidate, tmp_path, bank):
    fingerprint = hashlib.sha256(b"pairing-native-fixture").hexdigest()[:16]
    daemon = FixtureDaemon({"/health": json_body({"status": "ok", "auth": True, "schema": 59, "bank": fingerprint})})
    try:
        result = invoke(candidate, tmp_path, bank,
                        ["laptop", "--tier=core", "--no-board", "--someone-else", "--expires=1m", "--url", "http://example.com"],
                        port=daemon.server.server_address[1])
        succeeded = result.returncode == 0
        assert succeeded, "valid invite failed"
        import json
        report = json.loads(result.stdout)
        code = report["code"].replace("-", "")
        with _bank.connect(bank) as conn:
            row = conn.execute("SELECT code_hash, tier, board, owned FROM principals WHERE principal='laptop'").fetchone()
        matches = row == (hashlib.sha256(code.encode()).hexdigest(), "core", False, False)
        assert matches, "invite grants or code hash changed"
    finally:
        daemon.close()


@pytest.mark.parametrize(("fields", "message"), [
    ({"read_only": True},
     "the local daemon serves a read-only bank: run invite on the writable daemon host"),
    ({"version": "0.25.0", "db": None, "pending": ["init"]},
     "the daemon is still starting its storage: wait a few seconds, then re-run invite"),
    ({"version": "0.26.0"},
     "this daemon version does not give a fresh bank its identity at startup: update the daemon, then re-run invite"),
    ({"version": "0.25.0", "bank_identity_failed": False},
     "the daemon has no usable bank identity: check its startup logs before re-running invite"),
    ({"version": "0.25.0", "bank_identity_failed": False, "pending": ["bank_identity"]},
     "the daemon is still initializing its bank identity: wait a few seconds, then re-run invite"),
], ids=["read-only", "starting", "older-daemon", "missing-identity", "identity-pending"])
def test_invite_without_bank_identity_names_next_step(candidate, tmp_path, bank, fields, message):
    health = {"status": "ok", "auth": True, "schema": 59, "bank": None, "db": "ok", **fields}
    daemon = FixtureDaemon({"/health": json_body(health)})
    try:
        result = invoke(candidate, tmp_path, bank, ["laptop"], port=daemon.server.server_address[1])
        assert result.returncode == 4, "expected a native refusal"
        matches = json.loads(result.stdout).get("error") == message
        assert matches, "invite gave the wrong recovery step"
    finally:
        daemon.close()


@pytest.fixture
def native_daemon(candidate):
    suffix = ".exe" if os.name == "nt" else ""
    for name in ("pseudolife-daemon-static", "pseudolife-daemon"):
        binary = Path(candidate).with_name(name + suffix)
        if binary.is_file():
            return binary
    pytest.fail("build the native daemon beside the native client")


@pytest.fixture
def fresh_bank():
    name = _bank.name("pl_cf_pairing_fresh")
    _bank.create(name, schema=False)
    try:
        yield name
    finally:
        _bank.teardown(name)


def new_daemon(binary, home, bank, **overrides):
    from rust.daemon.harness import daemons

    data = home / "data"
    data.mkdir()
    # Storage opens before the embedder. This is an offline storage test.
    (data / "config.yaml").write_text(json.dumps({
        "embedding": {"model_name": str(data / "no-model")}, **overrides,
    }), encoding="utf-8")
    env = daemons.base_env(home, {
        "PSEUDOLIFE_MCP_HOST": "127.0.0.1",
        "PSEUDOLIFE_MCP_DATABASE_URL": _bank.url(bank),
        "PSEUDOLIFE_MCP_TOKEN": "synthetic-pairing-startup-test",
        "PSEUDOLIFE_RELEASE_CHECK": "0",
    })
    return daemons.rust_daemon(binary, home, daemons.free_port(), env)


def wait_for_daemon_health(daemon, predicate):
    from rust.harness_support.readiness import read_health

    if daemon.proc is None:
        daemon.start(stages=())
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and daemon.proc.poll() is None:
        try:
            status, body = read_health(f"http://127.0.0.1:{daemon.port}", timeout=3)
        except (OSError, http.client.HTTPException):
            pass
        else:
            if isinstance(body, dict) and predicate(status, body):
                return body
        time.sleep(0.1)
    pytest.fail("native daemon never reported the expected storage state", pytrace=False)


def health_after_storage_opens(daemon):
    return wait_for_daemon_health(daemon, lambda status, body: status == 200 and body.get("db") == "ok")


def health_after_bank_identity(daemon):
    return wait_for_daemon_health(daemon, lambda status, body:
                                  status == 200 and body.get("db") == "ok" and body.get("bank"))


def audit_identity(bank):
    with _bank.connect(bank) as conn:
        rows = conn.execute("SELECT value FROM meta WHERE key = 'coordination_bank_id'").fetchall()
        assert len(rows) == 1, "daemon startup left the bank without an identity"
        bank_id = rows[0][0]
        events = conn.execute("SELECT payload::jsonb FROM coordination_events WHERE event = 'bank_identity'").fetchall()
    assert events == [({"bank_id": bank_id},)]
    return bank_id


def test_move_counts_user_audit_rows_in_preflight_and_after_start(bank):
    # The move helpers include this same literal. `bank` owns a new database
    # per test, and the audit rows only exist in this session's temp table.
    predicate = (Path(__file__).resolve().parents[1] / "shim/src/cli/restore_cli/move_native"
                 / "coordination_events_filter.sql").read_text(encoding="utf-8")
    identity = '{"bank_id":"pairing-native-fixture"}'
    daemon_event = (1, "bank_identity", "daemon", "daemon", "", identity)
    context_event = (1, "bank_identity", "agent", "default", "", identity)
    cases = (
        ([daemon_event], 0),
        ([context_event], 0),
        ([context_event, (2, "register", "agent", "default", "agent-id", "{}")], 1),
        ([(1, "register", "daemon", "daemon", "", identity)], 1),
        ([(1, "bank_identity", "daemon", "daemon", "", '{"bank_id":"other"}')], 1),
        ([(1, "bank_identity", "daemon", "daemon", "", "{}")], 1),
    )
    with _bank.connect(bank) as conn:
        conn.execute("CREATE TEMP TABLE coordination_events "
                     "(seq bigint, event text, actor text, principal text, agent_id text, payload text)")
        for rows, expected in cases:
            with conn.transaction(force_rollback=True):
                with conn.cursor() as cursor:
                    cursor.executemany("INSERT INTO pg_temp.coordination_events VALUES (%s,%s,%s,%s,%s,%s)", rows)
                count = conn.execute(f"SELECT count(*) FROM pg_temp.coordination_events {predicate}").fetchone()[0]
                assert count == expected, rows


def test_fresh_daemon_reports_bank_identity_across_restart(native_daemon, tmp_path, fresh_bank):
    daemon = new_daemon(native_daemon, tmp_path, fresh_bank)
    try:
        health = health_after_bank_identity(daemon)
        identity = audit_identity(fresh_bank)
        assert health["bank"] == hashlib.sha256(identity.encode()).hexdigest()[:16]
        daemon.stop()
        restarted = health_after_bank_identity(daemon)
        assert restarted["bank"] == health["bank"]
        assert audit_identity(fresh_bank) == identity
    finally:
        daemon.stop()


@pytest.mark.parametrize("mode", ["config", "environment"])
def test_read_only_start_preserves_bank(candidate, native_daemon, tmp_path, fresh_bank, mode):
    _bank.create(fresh_bank)
    before = _bank.dump(fresh_bank)
    daemon = new_daemon(native_daemon, tmp_path, fresh_bank,
                        **({"read_only": True} if mode == "config" else {}))
    if mode == "environment":
        daemon.env["PSEUDOLIFE_READ_ONLY"] = "1"
    try:
        # The test login owns the fixture bank. A shadow must refuse these
        # credentials without opening writable storage or seeding the bank.
        health = wait_for_daemon_health(daemon, lambda status, body: status == 503 and
                                        body.get("init_refusal", "").startswith(
                                            "read-only shadow requires SELECT-only bank credentials"))
        assert health["read_only"] is True and health["bank"] is None
        result = invoke(candidate, tmp_path, fresh_bank, ["laptop"], port=daemon.port)
        assert result.returncode == 4
        matches = json.loads(result.stdout)["error"] == (
            "the local daemon serves a read-only bank: run invite on the writable daemon host")
        assert matches, "an unhealthy shadow gave the wrong invite remedy"
    finally:
        daemon.stop()
    assert _bank.dump(fresh_bank) == before


def test_first_invite_uses_fresh_daemon_bank(candidate, native_daemon, tmp_path, fresh_bank):
    daemon = new_daemon(native_daemon, tmp_path, fresh_bank)
    try:
        health_after_bank_identity(daemon)
        result = invoke(candidate, tmp_path, fresh_bank,
                        ["laptop", "--url", f"http://127.0.0.1:{daemon.port}"], port=daemon.port)
        # An assertion failure must not print a freshly minted code.
        succeeded = result.returncode == 0
        assert succeeded, "invite as the first command failed on a fresh daemon"
        report = json.loads(result.stdout)
        code = report["code"].replace("-", "")
        with _bank.connect(fresh_bank) as conn:
            row = conn.execute("SELECT code_hash FROM principals WHERE principal='laptop'").fetchone()
        matches = row == (hashlib.sha256(code.encode()).hexdigest(),)
        assert matches, "invite did not store the code in the daemon's bank"
    finally:
        daemon.stop()


def test_transient_bank_identity_failure_retries_startup(candidate, native_daemon, tmp_path, fresh_bank):
    _bank.create(fresh_bank)
    with _bank.connect(fresh_bank) as conn:
        conn.execute("""CREATE FUNCTION block_bank_identity() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.key = 'coordination_bank_id' THEN
                    RAISE EXCEPTION 'fixture identity lock unavailable' USING ERRCODE = '55P03';
                END IF;
                RETURN NEW;
            END $$;
            CREATE TRIGGER block_bank_identity BEFORE INSERT ON meta
                FOR EACH ROW EXECUTE FUNCTION block_bank_identity()""")
    daemon = new_daemon(native_daemon, tmp_path, fresh_bank)
    try:
        health = wait_for_daemon_health(daemon, lambda status, body:
                                        status == 200 and body.get("bank_identity_failed") is True)
        assert health["db"] == "ok"
        assert health["bank_identity_error"] == "coordination_unavailable"
        assert health["bank"] is None
        result = invoke(candidate, tmp_path, fresh_bank, ["laptop"], port=daemon.port)
        assert result.returncode == 4
        matches = json.loads(result.stdout)["error"] == (
            "the daemon could not initialize its bank identity: check its startup logs before re-running invite")
        assert matches, "identity initialization failure gave the wrong invite remedy"
        with _bank.connect(fresh_bank) as conn:
            conn.execute("DROP TRIGGER block_bank_identity ON meta")
        health = health_after_bank_identity(daemon)
        assert "bank_identity_failed" not in health
        assert health["bank"] == hashlib.sha256(audit_identity(fresh_bank).encode()).hexdigest()[:16]
        assert "bank identity initialization failed: PostgreSQL SQLSTATE 55P03" in daemon.log.read_text(encoding="utf-8")
    finally:
        daemon.stop()


@pytest.fixture
def healthy_daemon():
    fingerprint = hashlib.sha256(b"pairing-native-fixture").hexdigest()[:16]
    daemon = FixtureDaemon({"/health": json_body({"status": "ok", "auth": True, "schema": 59, "bank": fingerprint})})
    try:
        yield daemon.server.server_address[1]
    finally:
        daemon.close()


def test_forwarded_invite_url_placeholder_reaches_creation(candidate, tmp_path, bank, healthy_daemon):
    import json
    result = invoke(candidate, tmp_path, bank, ["laptop", "--url", "<daemon-url>"], port=healthy_daemon)
    succeeded = result.returncode == 0
    assert succeeded, "forwarded placeholder was refused"
    report = json.loads(result.stdout)
    placeholder_preserved = report.get("url") == "<daemon-url>"
    assert placeholder_preserved, "forwarded placeholder was replaced"
    note_present = any("put its URL in place of <daemon-url>" in note
                       for note in report.get("notes", []))
    assert note_present, "forwarded invite omitted its placeholder instruction"


@pytest.mark.parametrize("setting", ["PGAPPNAME", "PGCONNECT_TIMEOUT", "PGCLIENTENCODING", "PGDATESTYLE", "PGTZ", "PGGEQO"])
def test_nonpolicy_pg_setting_can_be_unset(candidate, tmp_path, bank, setting):
    env = environment(tmp_path)
    env.update(PSEUDOLIFE_MCP_DATABASE_URL=_bank.url(bank))
    env[setting] = "fixture-private-secret"
    result = subprocess.run([candidate, "invite", "--list", "--json"], env=env,
                            cwd=tmp_path, input=b"", capture_output=True, timeout=20)
    assert_refusal(result, "unset " + setting)
    assert invoke(candidate, tmp_path, bank, ["--list"]).returncode == 0


@pytest.mark.parametrize("option", ["application_name", "connect_timeout", "client_encoding"])
def test_nonpolicy_dsn_setting_can_be_removed(candidate, tmp_path, bank, option):
    env = environment(tmp_path)
    env["PSEUDOLIFE_MCP_DATABASE_URL"] = _bank.url(bank) + "?" + option + "=fixture-private-secret"
    result = subprocess.run([candidate, "invite", "--list", "--json"], env=env,
                            cwd=tmp_path, input=b"", capture_output=True, timeout=20)
    assert_refusal(result, "remove " + option)
    assert invoke(candidate, tmp_path, bank, ["--list"]).returncode == 0


def test_database_connection_failure_uses_database_exit(candidate, tmp_path):
    env = environment(tmp_path)
    env["PSEUDOLIFE_MCP_DATABASE_URL"] = "host=127.0.0.1 port=9 dbname=fixture user=fixture password=fixture-private-secret"
    result = subprocess.run([candidate, "invite", "--list", "--json"], env=env,
                            cwd=tmp_path, input=b"", capture_output=True, timeout=20)
    assert_refusal(result, "connection", code=1)


def test_lost_commit_reply_reports_possible_live_code(candidate, tmp_path, bank, healthy_daemon):
    """Forward the real COMMIT, then discard its success reply before the CLI sees it."""
    import select
    import socket
    import struct
    import threading
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    connection = conninfo_to_dict(_bank.url(bank))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(20)
    committed = threading.Event()
    failed = threading.Event()

    def relay():
        try:
            with listener.accept()[0] as client, socket.create_connection(
                    (connection["host"], int(connection["port"])), timeout=20) as server:
                # Startup has no message tag; later frontend frames do.
                startup = True
                pending = b""
                dropping = False
                replies = b""
                while True:
                    readable, _, _ = select.select([client, server], [], [], 20)
                    if not readable:
                        raise TimeoutError
                    for source in readable:
                        chunk = source.recv(65536)
                        if not chunk:
                            return
                        if source is client:
                            pending += chunk
                            while len(pending) >= (4 if startup else 5):
                                offset = 0 if startup else 1
                                size = struct.unpack("!I", pending[offset:offset + 4])[0] + offset
                                if len(pending) < size:
                                    break
                                frame, pending = pending[:size], pending[size:]
                                if not startup and frame[:1] == b"Q" and frame[5:].rstrip(b"\0").upper() == b"COMMIT":
                                    dropping = True
                                server.sendall(frame)
                                startup = False
                        elif not dropping:
                            client.sendall(chunk)
                        else:
                            replies += chunk
                            while len(replies) >= 5:
                                size = struct.unpack("!I", replies[1:5])[0] + 1
                                if len(replies) < size:
                                    break
                                frame, replies = replies[:size], replies[size:]
                                if frame[:1] == b"C" and frame[5:] == b"COMMIT\0":
                                    committed.set()
                                    return
        except (OSError, ValueError):
            failed.set()

    worker = threading.Thread(target=relay, daemon=True)
    worker.start()
    env = environment(tmp_path)
    env["PSEUDOLIFE_MCP_DATABASE_URL"] = make_conninfo(
        _bank.url(bank), host="127.0.0.1", port=listener.getsockname()[1], sslmode="disable")
    try:
        result = subprocess.run([candidate, "invite", "laptop", "--port", str(healthy_daemon), "--json"],
                                env=env, cwd=tmp_path, input=b"", capture_output=True, timeout=25)
    finally:
        listener.close()
        worker.join(timeout=25)
    assert not worker.is_alive() and not failed.is_set(), "commit fault injector failed"
    assert committed.is_set(), "fault did not occur after a real COMMIT"
    with _bank.connect(bank) as conn:
        live = conn.execute("SELECT code_hash IS NOT NULL FROM principals WHERE principal='laptop'").fetchone()
    assert live == (True,), "test did not leave the committed code in its disposable bank"
    assert_refusal(result, "a pairing code may have been created", code=1)
    actionable = b"invite --list" in result.stdout and b"invite --revoke" in result.stdout
    assert actionable, "ambiguous COMMIT has no check or revocation step"
    not_unchanged = b"Nothing was changed" not in result.stdout
    assert not_unchanged, "ambiguous COMMIT falsely promises no change"


@pytest.mark.parametrize("matches", [True, False])
def test_cleanup_fixture_recording_redacts_only_proven_file_token(matches):
    import base64
    import json
    from .rows import pair
    token = "a" * 43
    digest = hashlib.sha256((token if matches else "different").encode()).hexdigest()
    def encode(value):
        return base64.b64encode(value.encode()).decode()
    observation = {"files": {"pairing-12345678.token": "file:" + encode(token)},
                   "stdout": encode(token), "stderr": "",
                   "requests": [{"method": "POST", "body": json.dumps({"token_sha256": digest}),
                                 "headers": {"x-fixture-digest-of-a-token-file": "matches"}}]}
    # A leaked command-output token must remain visible to comparison, not be hidden.
    pair.cleanup_fixture_token(observation)
    expected = "<token>" if matches else token
    assert observation["files"]["pairing-12345678.token"] == "file:" + encode(expected)
    assert observation["stdout"] == encode(token)


@pytest.mark.parametrize("setting,representable", [
    ("PGHOST", True), ("PGPORT", True), ("PGDATABASE", True), ("PGUSER", True),
    ("PGSSLMODE", True), ("PGSSLROOTCERT", True),
    ("PGSERVICE", False), ("PGSSLCERT", False), ("PGSSLKEY", False),
    ("PGSSLCRL", False), ("PGSSLCRLDIR", False), ("PGCHANNELBINDING", False),
    ("PGTLS_UNSUPPORTED", False),
])
def test_unsupported_pg_setting_names_cause_without_value(candidate, tmp_path, bank, setting, representable):
    env = environment(tmp_path)
    env[setting] = "fixture-private-secret"
    env["PSEUDOLIFE_MCP_DATABASE_URL"] = "host=127.0.0.1 port=9 dbname=fixture user=fixture"
    result = subprocess.run([candidate, "invite", "--list", "--json"], env=env,
                            cwd=tmp_path, input=b"", capture_output=True, timeout=20)
    assert_refusal(result, setting if setting != "PGTLS_UNSUPPORTED" else "PGTLS")
    recovery = (b"PSEUDOLIFE_MCP_DATABASE_URL" if representable
                else b"ask the maintainer to add support")
    actionable = recovery in result.stdout
    assert actionable, "recovery requires a setting the client does not support"
    # The same connection with no unsupported ambient policy remains valid.
    result = invoke(candidate, tmp_path, bank, ["--list"])
    succeeded = result.returncode == 0
    assert succeeded, "ambient policy guard rejected an explicit valid connection"


@pytest.mark.parametrize("filename", ["postgresql.crt", "postgresql.key", "root.crl"])
def test_implicit_pg_certificate_names_file_without_contents(candidate, tmp_path, bank, filename):
    env = environment(tmp_path)
    directory = tmp_path / ("appdata/postgresql" if os.name == "nt" else ".postgresql")
    directory.mkdir(parents=True)
    (directory / filename).write_text("fixture-private-secret", encoding="utf-8")
    env["PSEUDOLIFE_MCP_DATABASE_URL"] = "host=127.0.0.1 port=9 dbname=fixture user=fixture"
    result = subprocess.run([candidate, "invite", "--list", "--json"], env=env,
                            cwd=tmp_path, input=b"", capture_output=True, timeout=20)
    assert_refusal(result, filename)
    actionable = b"ask the maintainer to add support" in result.stdout
    assert actionable, "required certificate policy was bypassed in recovery advice"
    (directory / filename).unlink()
    result = invoke(candidate, tmp_path, bank, ["--list"])
    succeeded = result.returncode == 0
    assert succeeded, "certificate policy guard blocked a profile with no such policy"


@pytest.mark.parametrize("option", ["sslcert", "sslkey", "sslcrl"])
def test_unsupported_dsn_policy_keeps_required_policy(candidate, tmp_path, bank, option):
    env = environment(tmp_path)
    env["PSEUDOLIFE_MCP_DATABASE_URL"] = _bank.url(bank) + "?" + option + "=fixture-private-secret"
    result = subprocess.run([candidate, "invite", "--list", "--json"], env=env,
                            cwd=tmp_path, input=b"", capture_output=True, timeout=20)
    assert_refusal(result, option)
    actionable = b"ask the maintainer to add support" in result.stdout
    assert actionable, "required DSN policy was bypassed in recovery advice"
    result = invoke(candidate, tmp_path, bank, ["--list"])
    succeeded = result.returncode == 0
    assert succeeded, "DSN policy guard rejected an ordinary connection"


@pytest.mark.parametrize("mode", ["invite", "pair"])
def test_nonunicode_argument_is_secret_free(candidate, tmp_path, mode):
    result = subprocess.run([candidate, mode, "fixture-private-secret" + chr(0xDCFF)],
                            env=environment(tmp_path), cwd=tmp_path, input=b"",
                            capture_output=True, timeout=20)
    diagnostic = result.stderr
    matches = (result.returncode == 2 and b"Unicode" in diagnostic
               and b"fixture-private-secret" not in diagnostic)
    assert matches, "non-Unicode argument did not receive a secret-free usage error"


def test_pair_missing_home_names_the_setting(candidate, tmp_path):
    env = environment(tmp_path)
    for key in ["HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"]:
        env.pop(key, None)
    result = subprocess.run([candidate, "pair", "http://127.0.0.1:9", "invalid", "--json"],
                            env=env, cwd=tmp_path, input=b"", capture_output=True, timeout=20)
    assert_refusal(result, "HOME")
    result = subprocess.run([candidate, "pair", "http://127.0.0.1:9", "invalid",
                             "--token-file", str(tmp_path / "new.token"), "--json"],
                            env=env, cwd=tmp_path, input=b"", capture_output=True, timeout=20)
    assert_refusal(result, "not a pairing code", code=2)


@pytest.mark.parametrize("depth", [987, 986])
def test_health_nesting_refusal_and_near_miss(candidate, tmp_path, depth):
    health = b'{"status":"ok","auth":true,"x":' + b'[' * depth + b']' * depth + b'}'
    daemon = FixtureDaemon({"/health": text(health, content_type="application/json")})
    try:
        result = subprocess.run([candidate, "pair", daemon.url, "ABCD-EFGH-JKMN", "--json"],
                                env=environment(tmp_path), cwd=tmp_path, input=b"",
                                capture_output=True, timeout=20)
        assert_refusal(result, "nesting limit" if depth == 987 else "HTTP 404")
        no_token = not list(tmp_path.rglob("*.token"))
        assert no_token, "refusal left a token file"
    finally:
        daemon.close()


@pytest.mark.parametrize("options,cause", [
    ("sslmode=allow", "sslmode"),
    ("host=/tmp/fixture-private-secret", "host"),
    ("port=fixture-private-secret", "port"),
    ("fixture_private_secret=unused", "unsupported connection option"),
    ("sslmode=verify-full sslrootcert=fixture-private-secret-missing", "sslrootcert"),
], ids=["sslmode", "socket-host", "port", "unknown-option", "missing-ca"])
def test_unsupported_dsn_option_is_secret_free(candidate, tmp_path, options, cause):
    env = environment(tmp_path)
    env["PSEUDOLIFE_MCP_DATABASE_URL"] = "host=127.0.0.1 port=9 dbname=fixture user=fixture " + options
    result = subprocess.run([candidate, "invite", "--list", "--json"], env=env,
                            cwd=tmp_path, input=b"", capture_output=True, timeout=20)
    assert_refusal(result, cause)


@pytest.mark.parametrize("field", ["auth", "schema", "bank", "bank-argument"])
def test_invite_diagnostics_do_not_echo_untrusted_health(candidate, tmp_path, bank, field):
    fingerprint = hashlib.sha256(b"pairing-native-fixture").hexdigest()[:16]
    health = {"status": "ok", "auth": True, "schema": 59, "bank": fingerprint}
    args = ["laptop"]
    if field == "bank-argument":
        args += ["--bank", "fixture-private-secret"]
    else:
        health[field] = "fixture-private-secret"
    daemon = FixtureDaemon({"/health": json_body(health)})
    try:
        before = _bank.dump(bank)
        result = invoke(candidate, tmp_path, bank, args, port=daemon.server.server_address[1])
        secret_absent = b"fixture-private-secret" not in result.stdout + result.stderr
        assert secret_absent, "invite diagnostic exposed an untrusted value"
        unchanged = before == _bank.dump(bank)
        assert unchanged, "invalid health or bank confirmation changed the bank"
    finally:
        daemon.close()


def test_pair_diagnostics_do_not_echo_untrusted_auth(candidate, tmp_path):
    daemon = FixtureDaemon({"/health": json_body({"status": "ok", "auth": "fixture-private-secret"})})
    try:
        result = subprocess.run([candidate, "pair", daemon.url, "ABCD-EFGH-JKMN", "--json"],
                                env=environment(tmp_path), cwd=tmp_path, input=b"",
                                capture_output=True, timeout=20)
        secret_absent = b"fixture-private-secret" not in result.stdout + result.stderr
        assert secret_absent, "pair diagnostic exposed an untrusted auth value"
        no_token = not list(tmp_path.rglob("*.token"))
        assert no_token, "unverified auth left a token"
    finally:
        daemon.close()


# These failure mechanisms are Unix resource limits, so Windows does not collect
# them. Its locked-file cleanup test lives in cli_pairing_contract.rs.
if os.name != "nt":
    @pytest.mark.parametrize("resource_name,limit,cause", [
        ("RLIMIT_NPROC", 0, "worker could not start"),
    ])
    def test_pairing_resource_failure_is_native(candidate, tmp_path, resource_name, limit, cause):
        import resource

        def limit_child():
            resource.setrlimit(getattr(resource, resource_name), (limit, limit))

        result = subprocess.run([candidate, "pair", "http://127.0.0.1:9", "invalid", "--json"],
                                env=environment(tmp_path), cwd=tmp_path, input=b"",
                                capture_output=True, timeout=20, preexec_fn=limit_child)
        assert_refusal(result, cause)

    def test_unreadable_code_input_is_native(candidate, tmp_path):
        descriptor = os.open(tmp_path, os.O_RDONLY)
        try:
            result = subprocess.run([candidate, "pair", "http://127.0.0.1:9", "--read-code", "--json"],
                                    env=environment(tmp_path), cwd=tmp_path, stdin=descriptor,
                                    capture_output=True, timeout=20)
        finally:
            os.close(descriptor)
        assert_refusal(result, "--read-code could not read stdin")



def test_unresolvable_token_path_names_parameter(candidate, tmp_path):
    result = subprocess.run([candidate, "pair", "http://127.0.0.1:9", "invalid",
                             "--token-file", "~fixture-private-secret-user/new.token", "--json"],
                            env=environment(tmp_path), cwd=tmp_path, input=b"",
                            capture_output=True, timeout=20)
    assert_refusal(result, "--token-file path could not be expanded")
    result = subprocess.run([candidate, "pair", "http://127.0.0.1:9", "invalid",
                             "--token-file", "~/new.token", "--json"],
                            env=environment(tmp_path), cwd=tmp_path, input=b"",
                            capture_output=True, timeout=20)
    assert_refusal(result, "not a pairing code", code=2)


if os.name != "nt":
    def test_uninspectable_token_target_names_parameter(candidate, tmp_path):
        parent = tmp_path / "parent"
        parent.mkdir(mode=0o700)
        argv = [candidate, "pair", "http://127.0.0.1:9", "ABCD-EFGH-JKMN",
                "--token-file", str(parent / "new.token"), "--json"]
        parent.chmod(0)
        try:
            result = subprocess.run(argv, env=environment(tmp_path), cwd=tmp_path,
                                    input=b"", capture_output=True, timeout=20)
        finally:
            parent.chmod(0o700)
        assert_refusal(result, "--token-file destination could not be inspected")
        result = subprocess.run(argv, env=environment(tmp_path), cwd=tmp_path,
                                input=b"", capture_output=True, timeout=20)
        assert_refusal(result, "/health")

    def test_nonunicode_resolved_token_path_is_refused(candidate, tmp_path):
        cwd = tmp_path / chr(0xDCFF)
        cwd.mkdir()
        result = subprocess.run([candidate, "pair", "http://127.0.0.1:9", "ABCD-EFGH-JKMN",
                                 "--token-file", "new.token", "--json"],
                                env=environment(tmp_path), cwd=cwd, input=b"",
                                capture_output=True, timeout=20)
        assert_refusal(result, "--token-file destination is not valid Unicode")
