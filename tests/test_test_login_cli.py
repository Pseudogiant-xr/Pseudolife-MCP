"""``pseudolife-mcp test-login create``: a test-suite login that cannot open
the bank (2026-10-04).

The unit half runs everywhere. The server half provisions the login on a
disposable server named by ``PSEUDOLIFE_TEST_LOGIN_ADMIN_URL`` (a superuser
URL; CI's service container, or a throwaway container of your own) and proves
what the suite needs: the login creates its own database, the schema installs
there (``vector`` comes from template1), the fixtures' resets and drops work,
a real slice of PG-backed test files passes under it, and it cannot connect
to the production-named database. It skips without that variable, and
refuses a server whose production database holds a bank: never point it at
the live server.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from pseudolife_memory import test_login_cli as cli

ROOT = Path(__file__).resolve().parent.parent


# -- the password never reaches the server: only its SCRAM verifier -----------

def test_the_verifier_matches_rfc_7677():
    """RFC 7677's SCRAM-SHA-256 exchange (user "user", password "pencil"):
    the stored key must verify the client's proof and the server key must
    produce the server's signature."""
    salt = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
    verifier = cli.scram_verifier("pencil", salt=salt, iterations=4096)
    method, _, rest = verifier.partition("$")
    params, _, keys = rest.partition("$")
    assert method == "SCRAM-SHA-256"
    assert params == "4096:W22ZaJ0SNY7soEsUEjb6gQ=="
    stored_key, server_key = (base64.b64decode(part) for part in keys.split(":"))
    auth = (b"n=user,r=rOprNGfwEbeRWgbNEkqO,"
            b"r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,"
            b"s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,"
            b"c=biws,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0")
    server_signature = hmac.new(server_key, auth, "sha256").digest()
    assert base64.b64encode(server_signature) == b"6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="
    proof = base64.b64decode("dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ=")
    client_signature = hmac.new(stored_key, auth, "sha256").digest()
    client_key = bytes(a ^ b for a, b in zip(proof, client_signature))
    assert hashlib.sha256(client_key).digest() == stored_key


def test_the_role_statements_carry_the_verifier_and_never_the_password():
    password = "never-in-sql-" + "x" * 20
    statements = cli.role_statements("pseudolife_test", cli.scram_verifier(password),
                                     ["pseudolife_memory"])
    sql = "\n".join(statements)
    assert password not in sql
    assert "SCRAM-SHA-256$" in sql
    assert "NOSUPERUSER" in sql and "CREATEDB" in sql and "NOCREATEROLE" in sql
    assert 'REVOKE CONNECT ON DATABASE "pseudolife_memory" FROM PUBLIC' in sql


def test_database_names_are_quoted_as_identifiers():
    sql = "\n".join(cli.role_statements("pseudolife_test", "SCRAM-SHA-256$1:a$b:c",
                                        ['odd"name']))
    assert 'ON DATABASE "odd""name" FROM PUBLIC' in sql


@pytest.mark.parametrize("name", ["", "Pseudolife", "a-b", "x;drop", "1abc", "a" * 64])
def test_a_role_name_outside_the_safe_shape_is_refused(name, tmp_path):
    out = io.StringIO()
    code = cli.main(["create", "--role", name, "--file", str(tmp_path / "f.env")],
                    out=out, executor=_FakeServer())
    assert code == cli.EXIT_USAGE


def test_the_default_file_is_the_one_the_suite_reads():
    from tests import pg_defaults

    assert cli.DEFAULT_FILE == pg_defaults.LOGIN_FILE
    assert cli.FILE_ENV == pg_defaults.LOGIN_FILE_ENV
    assert cli.DEFAULT_ROLE == pg_defaults.TEST_LOGIN_ROLE


def test_the_default_banks_are_the_production_databases():
    from pseudolife_memory.storage.schema import PRODUCTION_DATABASES

    assert set(cli.DEFAULT_BANKS) == set(PRODUCTION_DATABASES)


def test_every_extension_the_schema_creates_is_preinstalled():
    """CREATE EXTENSION of an untrusted extension needs a superuser. The test
    login has none, so every extension ensure_schema creates must already be
    in template1, which CREATE DATABASE copies."""
    import re

    schema = (ROOT / "pseudolife_memory" / "storage" / "schema.py").read_text(encoding="utf-8")
    created = set(re.findall(r"CREATE EXTENSION IF NOT EXISTS (\w+)", schema))
    assert created and created <= set(cli.EXTENSIONS)


def test_every_bulk_reap_spares_the_servers_own_workers():
    """The per-test reset reaps every other backend on its database. As the
    owner (a superuser) that also killed autovacuum workers; as the test
    login PostgreSQL 18 refuses ("Only roles with privileges of the
    pg_signal_autovacuum_worker role may terminate autovacuum workers"),
    which ERRORed a test whenever autovacuum was on the database (scratch
    server, 2026-10-04). The reaps are for leaked client connections."""
    import re

    reap = re.compile(r'"SELECT pg_terminate_backend\(pid\) FROM pg_stat_activity "'
                      r'(?:\s*"[^"]*")*')
    found = []
    for folder in ("tests", "evals"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            for match in reap.finditer(path.read_text(encoding="utf-8")):
                found.append((path.relative_to(ROOT).as_posix(), match.group(0)))
    assert found
    unfiltered = [name for name, statement in found
                  if "backend_type = 'client backend'" not in statement]
    assert not unfiltered, unfiltered


# -- the command against a recorded server --------------------------------------

class _FakeServer:
    """Answers the command's queries the way a server would, and records them."""

    def __init__(self, *, superuser=True, user="pseudolife", database="pseudolife_memory",
                 role=None, owns=(), fail_on=None):
        self.superuser, self.user, self.database = superuser, user, database
        self.role, self.owns, self.fail_on = role, list(owns), fail_on
        self.calls: list[tuple[str | None, list[str]]] = []
        self.applied = False
        self.vector = ""
        self.description = "a recorded server"

    def query(self, database, statements):
        self.calls.append((database, list(statements)))
        sql = "\n".join(statements)
        if self.fail_on and self.fail_on in sql:
            raise cli.DatabaseError("the server said no")
        if "'who'" in sql:
            return json.dumps({"who": self.user, "super": self.superuser, "db": self.database})
        if "'banks'" in sql:
            role = None
            if self.applied:
                role = {"super": False, "login": True, "createdb": True, "createrole": False,
                        "replication": False, "bypassrls": False}
            elif self.role is not None:
                role = self.role
            banks = {"pseudolife_memory": {
                "owner": "pseudolife", "public_connect": not self.applied,
                "role_connect": False if self.applied else role is not None,
                "owner_connect": True}}
            return json.dumps({"role": role, "member_of": [], "banks": banks,
                               "owns": self.owns, "others": ["pseudolife_memory_bench"]})
        if "pg_extension" in sql and "CREATE EXTENSION" not in sql:
            return self.vector
        if "CREATE EXTENSION" in sql:
            self.vector = "vector 0.8.6"
            return self.vector
        if "CREATE ROLE" in sql:
            self.applied = True
            return "ok"
        raise AssertionError(f"unexpected query: {sql}")


def _run(args, server, tmp_path):
    out = io.StringIO()
    code = cli.main(["create", "--file", str(tmp_path / "test-pg.env"), *args],
                    out=out, executor=server)
    return code, out.getvalue()


def test_create_provisions_closes_the_bank_and_writes_the_file(tmp_path):
    server = _FakeServer()
    code, out = _run([], server, tmp_path)
    assert code == cli.EXIT_OK, out
    text = (tmp_path / "test-pg.env").read_text(encoding="utf-8")
    assert "PSEUDOLIFE_TEST_PG_USER=pseudolife_test\n" in text
    password = text.split("PSEUDOLIFE_TEST_PG_PASSWORD=", 1)[1].strip()
    assert len(password) >= 32
    assert password not in out
    assert "created" in out
    assert "pseudolife_memory: CONNECT revoked from PUBLIC" in out
    assert "template1: installed vector 0.8.6" in out
    assert "pseudolife_memory_bench" in out  # named, not changed
    for database, statements in server.calls:
        assert password not in "\n".join(statements)
    assert [db for db, _ in server.calls if any("CREATE EXTENSION" in s for s in _)] == ["template1"]


def test_a_second_run_reapplies_the_files_password_and_rotate_replaces_it(tmp_path):
    _run([], _FakeServer(), tmp_path)
    first = (tmp_path / "test-pg.env").read_text(encoding="utf-8")
    code, out = _run([], _FakeServer(role={"super": False}), tmp_path)
    assert code == cli.EXIT_OK, out
    assert (tmp_path / "test-pg.env").read_text(encoding="utf-8") == first
    assert "re-applied" in out
    code, out = _run(["--rotate"], _FakeServer(role={"super": False}), tmp_path)
    assert code == cli.EXIT_OK, out
    assert (tmp_path / "test-pg.env").read_text(encoding="utf-8") != first
    assert "rotated" in out


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_the_file_is_owner_only(tmp_path):
    _run([], _FakeServer(), tmp_path)
    assert (tmp_path / "test-pg.env").stat().st_mode & 0o077 == 0


def test_a_connection_without_superuser_is_refused_before_any_change(tmp_path):
    server = _FakeServer(superuser=False, user="someone")
    code, out = _run([], server, tmp_path)
    assert code == cli.EXIT_REFUSED
    assert "superuser" in out
    assert len(server.calls) == 1
    assert not (tmp_path / "test-pg.env").exists()


def test_the_owner_role_itself_is_refused(tmp_path):
    code, out = _run(["--role", "pseudolife"], _FakeServer(), tmp_path)
    assert code == cli.EXIT_REFUSED
    assert not (tmp_path / "test-pg.env").exists()


@pytest.mark.parametrize("server", [
    _FakeServer(role={"super": True}),
    _FakeServer(role={"super": False}, owns=["pseudolife_memory"]),
], ids=["superuser", "owns-the-bank"])
def test_an_existing_role_that_is_not_a_test_login_is_refused(server, tmp_path):
    code, out = _run([], server, tmp_path)
    assert code == cli.EXIT_REFUSED, out
    assert not server.applied
    assert not (tmp_path / "test-pg.env").exists()


def test_a_failed_role_change_leaves_no_file(tmp_path):
    server = _FakeServer(fail_on="CREATE ROLE")
    code, out = _run([], server, tmp_path)
    assert code == cli.EXIT_FAILED
    assert not list(tmp_path.iterdir())


def test_template_banks_and_the_admin_database_cannot_be_named(tmp_path):
    for name in ("postgres", "template1", "template0"):
        code, _ = _run(["--bank", name], _FakeServer(), tmp_path)
        assert code == cli.EXIT_USAGE


def test_without_an_admin_url_it_goes_through_the_postgres_container(monkeypatch):
    seen = []

    def run(cmd, input=None):
        seen.append((cmd, input))
        return subprocess.CompletedProcess(cmd, 0, "line one\n42\n", "")

    monkeypatch.setattr(cli, "run", run)
    executor = cli.ContainerPsql("pseudolife-mcp-postgres")
    assert executor.query("template1", ["SELECT 42"]) == "42"
    cmd, script = seen[0]
    assert cmd[:4] == ["docker", "exec", "-i", "pseudolife-mcp-postgres"]
    assert cmd[-1] == "template1"
    assert '"$POSTGRES_USER"' in cmd[-3]  # the container's own superuser, over its socket
    assert "SELECT 42" in script  # SQL on stdin, never on a command line
    executor.query(None, ["SELECT 1"])
    assert seen[1][0][-1] == ""  # the container's POSTGRES_DB


def test_a_failing_psql_raises_with_its_message(monkeypatch):
    monkeypatch.setattr(cli, "run", lambda cmd, input=None: subprocess.CompletedProcess(
        cmd, 1, "", "ERROR:  boom"))
    with pytest.raises(cli.DatabaseError, match="boom"):
        cli.ContainerPsql("c").query(None, ["SELECT 1"])


def test_the_command_is_dispatched_and_listed():
    from pseudolife_memory import cli as dispatch

    assert "test-login" in dispatch._USAGE
    proc = subprocess.run([sys.executable, "-m", "pseudolife_memory.cli", "test-login", "--help"],
                          cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "create" in proc.stdout


# -- the server half: a disposable server only ----------------------------------

ADMIN_ENV = "PSEUDOLIFE_TEST_LOGIN_ADMIN_URL"


@pytest.fixture(scope="module")
def scratch_admin():
    url = os.environ.get(ADMIN_ENV)
    if not url:
        pytest.skip(f"{ADMIN_ENV} names no disposable server")
    psycopg = pytest.importorskip("psycopg")
    from tests.pg_defaults import RedactedUrl, conninfo_with_dbname

    url = RedactedUrl(url)
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        names = {row[0] for row in conn.execute("SELECT datname FROM pg_database")}
        created = "pseudolife_memory" not in names
        if created:
            conn.execute('CREATE DATABASE "pseudolife_memory"')
    bank = RedactedUrl(conninfo_with_dbname(url, "pseudolife_memory"))
    with psycopg.connect(bank, autocommit=True, connect_timeout=5) as conn:
        tables = conn.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
                              ).fetchone()[0]
    if tables:
        pytest.fail(f"{ADMIN_ENV} names a server whose pseudolife_memory holds "
                    f"{tables} tables: a real bank. Point it at a disposable server.")
    yield url
    # Leave the server as found, but for vector in template1: the login and
    # the empty production-named database this module made.
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        try:
            conn.execute(f"DROP ROLE IF EXISTS {cli.DEFAULT_ROLE}")
        except psycopg.Error:
            pass  # it still owns a database a failed slice leaked
        if created:
            conn.execute('DROP DATABASE IF EXISTS "pseudolife_memory" WITH (FORCE)')


@pytest.fixture(scope="module")
def provisioned(scratch_admin, tmp_path_factory):
    login_file = tmp_path_factory.mktemp("test-login") / "test-pg.env"
    out = io.StringIO()
    code = cli.main(["create", "--admin-url", scratch_admin, "--file", str(login_file)],
                    out=out)
    assert code == cli.EXIT_OK, out.getvalue()
    # Idempotent: a second run re-applies the same password and changes nothing.
    again = io.StringIO()
    assert cli.main(["create", "--admin-url", scratch_admin, "--file", str(login_file)],
                    out=again) == cli.EXIT_OK, again.getvalue()
    assert "re-applied" in again.getvalue()
    assert "already closed to PUBLIC" in again.getvalue()
    from tests.pg_defaults import login_file_credentials

    user, password = login_file_credentials(login_file)
    from psycopg.conninfo import conninfo_to_dict

    parts = conninfo_to_dict(scratch_admin)
    host_port = f"{parts.get('host', '127.0.0.1')}:{parts.get('port', '5432')}"
    return {"admin": scratch_admin, "file": login_file, "user": user,
            "password": password, "host_port": host_port}


def _login_url(provisioned, database):
    from urllib.parse import quote

    return (f"postgresql://{provisioned['user']}:{quote(provisioned['password'], safe='')}"
            f"@{provisioned['host_port']}/{database}")


def test_the_login_cannot_connect_to_the_bank_and_the_owner_still_can(provisioned):
    import psycopg
    from tests.pg_defaults import RedactedUrl, conninfo_with_dbname

    with pytest.raises(psycopg.OperationalError) as refused:
        psycopg.connect(RedactedUrl(_login_url(provisioned, "pseudolife_memory")),
                        connect_timeout=5)
    assert "permission denied for database" in str(refused.value)
    owner = RedactedUrl(conninfo_with_dbname(provisioned["admin"], "pseudolife_memory"))
    with psycopg.connect(owner, connect_timeout=5) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)


def test_the_login_holds_no_privilege_beyond_createdb(provisioned):
    import psycopg
    from tests.pg_defaults import RedactedUrl

    with psycopg.connect(RedactedUrl(_login_url(provisioned, "postgres")),
                         autocommit=True, connect_timeout=5) as conn:
        row = conn.execute(
            "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls, "
            "(SELECT count(*) FROM pg_auth_members WHERE member = r.oid) "
            "FROM pg_roles r WHERE rolname = current_user").fetchone()
        assert row == (False, False, True, False, False, 0)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE ROLE pl_test_login_escalation")


def test_the_login_runs_the_suites_database_lifecycle(provisioned):
    """What tests/pg_fixtures.py does, as the login: create a private
    database, install the schema (vector comes from template1), reap other
    backends, truncate every reset table, drop the database."""
    import psycopg
    from pseudolife_memory.storage.postgres import PostgresStorage
    from pseudolife_memory.storage.schema import BENCH_RESET_TABLES, assert_disposable_database
    from tests.pg_defaults import RedactedUrl

    name = f"pseudolife_memory_test_login_{os.getpid()}"
    admin = RedactedUrl(_login_url(provisioned, "postgres"))
    with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        url = RedactedUrl(_login_url(provisioned, name))
        storage = PostgresStorage(url)  # ensure_schema, CREATE EXTENSION included
        storage.close()
        with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
            assert_disposable_database(conn)
            conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                         "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                         "AND backend_type = 'client backend'")
            conn.execute("TRUNCATE " + ", ".join(BENCH_RESET_TABLES) + " CASCADE")
    finally:
        with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


# A slice of the real suite under the login: the per-run database fixtures
# (create, reset, prune, drop), storage, the coordination store, principals,
# a daemon on its own database, and backend reaping.
SLICE = (
    "tests/test_pg_run_isolation.py",
    "tests/test_pg_storage.py",
    "tests/test_principal_store_pg.py",
    "tests/test_connection_loss_recovery.py",
    "tests/test_coordination_leases.py",
)


def test_a_slice_of_the_suite_passes_under_the_login(provisioned, tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in (
        "PSEUDOLIFE_TEST_DATABASE_URL", "PSEUDOLIFE_BENCH_ADMIN_URL",
        "_PSEUDOLIFE_BENCH_ADMIN_URL_SEEDED", "PSEUDOLIFE_TEST_PG_PASSWORD",
        "PSEUDOLIFE_TEST_PG_USER", "PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_ADDOPTS")}
    env.update({
        "PSEUDOLIFE_TEST_PG_HOST_PORT": provisioned["host_port"],
        "PSEUDOLIFE_TEST_PG_LOGIN_FILE": str(provisioned["file"]),
        "PSEUDOLIFE_REQUIRE_TEST_POSTGRES": "1",
        "PSEUDOLIFE_SUITE_LOCK": "off",
    })
    log = tmp_path / "slice.log"
    with log.open("w", encoding="utf-8") as fh:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", *SLICE, "-q", "-p", "no:cacheprovider",
             "-p", "no:xdist", "-rs"],
            cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT, timeout=1800)
    text = log.read_text(encoding="utf-8", errors="replace")
    assert proc.returncode == 0, text[-4000:]
    assert " passed" in text and " skipped" not in text.splitlines()[-1], text[-2000:]
