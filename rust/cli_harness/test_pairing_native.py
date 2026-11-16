"""Native pairing refusals against an isolated test PostgreSQL and HTTP fixture."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess

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
