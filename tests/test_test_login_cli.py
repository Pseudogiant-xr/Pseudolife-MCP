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
                 role=None, owns=(), fail_on=None, daemon=None, leftovers=()):
        self.superuser, self.user, self.database = superuser, user, database
        self.role, self.owns, self.fail_on = role, list(owns), fail_on
        self.daemon, self.leftovers = daemon, list(leftovers)
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
                               "owns": self.owns, "others": ["pseudolife_memory_bench"],
                               "daemon": self.daemon,
                               "template1_public_connect": not self.applied,
                               "leftovers": [] if self.applied else self.leftovers})
        if "CREATE EXTENSION" in sql:
            before, self.vector = self.vector, "vector 0.8.6"
            return f"{before}|{self.vector}"
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


@pytest.mark.parametrize("file_text", [None, "PSEUDOLIFE_TEST_PG_USER=other_role\n"
                                              "PSEUDOLIFE_TEST_PG_PASSWORD=x\n"],
                         ids=["no-file", "another-roles-file"])
def test_an_existing_role_without_its_file_is_not_silently_given_a_new_password(
        tmp_path, file_text):
    """A first run on another account (or after the file was lost) would
    draw a new password and break every other copy of the file."""
    if file_text is not None:
        (tmp_path / "test-pg.env").write_text(file_text, encoding="utf-8")
    server = _FakeServer(role={"super": False})
    code, out = _run([], server, tmp_path)
    assert code == cli.EXIT_REFUSED, out
    assert "--rotate" in out and "stop working" in out
    assert not server.applied
    assert (tmp_path / "test-pg.env").exists() == (file_text is not None)
    code, out = _run(["--rotate"], _FakeServer(role={"super": False}), tmp_path)
    assert code == cli.EXIT_OK, out
    assert "rotated" in out


DAEMON_DSN = "postgresql://memapp:daemon-secret-xyz@db.example.com:5432/pseudolife_memory"


def test_a_daemon_user_that_would_lose_connect_is_refused_before_any_change(
        tmp_path, monkeypatch):
    """A daemon that logs in as a non-owner, non-superuser role reaches the
    bank only through PUBLIC's CONNECT: the revoke would lock it out."""
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", DAEMON_DSN)
    server = _FakeServer(daemon={"exists": True, "keeps": {"pseudolife_memory": False}})
    code, out = _run([], server, tmp_path)
    assert code == cli.EXIT_REFUSED, out
    assert 'GRANT CONNECT ON DATABASE "pseudolife_memory" TO "memapp"' in out
    assert "daemon-secret-xyz" not in out
    assert not server.applied
    assert not (tmp_path / "test-pg.env").exists()
    state = [s for _, statements in server.calls for s in statements if "'banks'" in s]
    assert state and "'memapp'" in state[0]


@pytest.mark.parametrize("daemon,said", [
    ({"exists": True, "keeps": {"pseudolife_memory": True}}, "memapp"),
    ({"exists": False, "keeps": {}}, "not a role on this server"),
], ids=["keeps-connect", "not-on-this-server"])
def test_a_daemon_user_that_keeps_connect_goes_ahead(tmp_path, monkeypatch, daemon, said):
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", DAEMON_DSN)
    code, out = _run([], _FakeServer(daemon=daemon), tmp_path)
    assert code == cli.EXIT_OK, out
    assert said in out
    assert "daemon-secret-xyz" not in out


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


class _GarbledServer(_FakeServer):
    def query(self, database, statements):
        self.calls.append((database, list(statements)))
        return "psql: something that is not JSON"


def test_an_answer_that_is_not_json_fails_cleanly(tmp_path):
    code, out = _run([], _GarbledServer(), tmp_path)
    assert code == cli.EXIT_FAILED
    assert "test-login:" in out
    assert not (tmp_path / "test-pg.env").exists()


LEFTOVERS = ["pseudolife_memory_test_4242", "pseudolife_memory_bench_wsl77",
             "pseudolife_memory_bench_audit_913"]


def test_leftover_test_databases_are_handed_to_the_login(tmp_path):
    """A hard-killed run as the bank owner leaves its per-run database; the
    test login's prune cannot drop a database it does not own."""
    server = _FakeServer(leftovers=[*LEFTOVERS, "pseudolife_memory", "pseudolife_memory_bench",
                                    "pseudolife_memory_test_x", "mydb_test_1"])
    code, out = _run([], server, tmp_path)
    assert code == cli.EXIT_OK, out
    sql = "\n".join(s for _, statements in server.calls for s in statements)
    for name in LEFTOVERS:
        assert f'ALTER DATABASE "{name}" OWNER TO "pseudolife_test"' in sql
        assert f"database {name}: a leftover test database, handed to pseudolife_test" in out
    for name in ("pseudolife_memory", "pseudolife_memory_bench", "pseudolife_memory_test_x",
                 "mydb_test_1"):
        assert f'ALTER DATABASE "{name}"' not in sql


@pytest.mark.parametrize("name,matches", [
    ("pseudolife_memory_test_4242", True), ("pseudolife_memory_bench_wsl77", True),
    ("pseudolife_memory_bench_audit_913", True), ("pseudolife_memory_test_proof_12", True),
    ("pseudolife_memory", False), ("pseudolife_memory_bench", False),
    ("pseudolife_memory_test", False), ("pseudolife_memory_test_gw0", False),
    ("pseudolife_memory_test_x", False), ("xpseudolife_memory_test_1", False),
])
def test_the_leftover_pattern_is_the_pruners(name, matches):
    """tests/pg_fixtures.py prunes `pseudolife_memory_test_*` and
    `pseudolife_memory_bench_*` whose last segment is a run's pid."""
    from tests.pg_defaults import own_run_pid

    assert bool(cli.LEFTOVER_DATABASE.fullmatch(name)) is matches
    pruner = (name.startswith(("pseudolife_memory_test_", "pseudolife_memory_bench_"))
              and any(own_run_pid(name.rsplit("_", 1)[1], namespace=ns) is not None
                      for ns in ("", "wsl")))
    assert pruner is matches


def test_template1_is_closed_to_public(tmp_path):
    """A login connected to template1 makes every CREATE DATABASE that copies
    it fail, the owner's after a restore's DROP included. Nothing needs
    PUBLIC there: CREATE DATABASE copies a template it cannot connect to."""
    sql = "\n".join(cli.role_statements("pseudolife_test", "SCRAM-SHA-256$1:a$b:c", []))
    assert 'REVOKE CONNECT ON DATABASE "template1" FROM PUBLIC' in sql
    code, out = _run([], _FakeServer(), tmp_path)
    assert code == cli.EXIT_OK, out
    assert "template1: CONNECT revoked from PUBLIC" in out
    problems = cli._verify({"role": {"super": False, "login": True, "createdb": True,
                                     "createrole": False, "replication": False,
                                     "bypassrls": False},
                            "template1_public_connect": True}, "pseudolife_test", [])
    assert problems == ["PUBLIC can still connect to template1"]


def test_template1_is_opened_once(tmp_path):
    """CREATE DATABASE fails while another session is connected to its
    template, so a concurrent test run's database creation can collide with
    this command: it holds one short connection to template1, not two."""
    server = _FakeServer()
    code, out = _run([], server, tmp_path)
    assert code == cli.EXIT_OK, out
    assert [db for db, _ in server.calls].count("template1") == 1


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


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_a_bad_admin_url_never_prints_its_password(tmp_path, as_json):
    """psycopg quotes the token it cannot decode, here the URL's password
    (review, 2026-10-04: `invalid percent-encoded token: "<password>%zz"`)."""
    secret = "FAKESECRET" + "q" * 12
    out = io.StringIO()
    code = cli.main(["create", "--admin-url", f"postgresql://admin:{secret}%zz@127.0.0.1:1/postgres",
                     "--file", str(tmp_path / "f.env"), *(["--json"] if as_json else [])], out=out)
    assert code == cli.EXIT_FAILED
    assert "test-login" in out.getvalue() or as_json
    assert secret not in out.getvalue(), out.getvalue()


@pytest.mark.parametrize("text", [
    'invalid percent-encoded token: "s3cr%zz"',
    "could not parse postgresql://admin:s3cr%zz@host/db",
    "bad conninfo: host=h password=s3cr%zz user=u",
    "bad conninfo: host=h password='s3cr%zz' user=u",
])
def test_error_text_is_masked_for_the_urls_password_and_any_password_shaped_token(text):
    masked = cli.mask_secrets(text, "postgresql://admin:s3cr%zz@host/db")
    assert "s3cr" not in masked, masked
    masked = cli.mask_secrets(text, None)  # no URL to learn the password from
    assert "s3cr" not in masked, masked


def test_the_admin_url_needs_no_password_on_the_command_line(tmp_path, monkeypatch):
    """libpq reads PGPASSWORD and ~/.pgpass, so the runbook's form keeps the
    superuser's password out of argv and shell history."""
    seen = {}

    class _Probe(cli.AdminUrl):
        def query(self, database, statements):
            seen["url"] = self._url
            raise cli.DatabaseError("stop here")

    monkeypatch.setattr(cli, "AdminUrl", _Probe)
    code = cli.main(["create", "--admin-url", "postgresql://admin@127.0.0.1:1/postgres",
                     "--file", str(tmp_path / "f.env")], out=io.StringIO())
    assert code == cli.EXIT_FAILED
    assert seen["url"] == "postgresql://admin@127.0.0.1:1/postgres"


def test_the_command_is_dispatched_and_listed():
    from pseudolife_memory import cli as dispatch

    assert "test-login" in dispatch._USAGE
    proc = subprocess.run([sys.executable, "-m", "pseudolife_memory.cli", "test-login", "--help"],
                          cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "create" in proc.stdout


# -- the server half: a disposable server only ----------------------------------

ADMIN_ENV = "PSEUDOLIFE_TEST_LOGIN_ADMIN_URL"
# Not the default role: on a server where `test-login create` was run for
# real, this module's teardown must not drop that login.
CI_ROLE = "pseudolife_test_ci"


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
        template1_open = conn.execute(
            "SELECT has_database_privilege('public', 'template1', 'CONNECT')").fetchone()[0]
        if created:
            conn.execute('CREATE DATABASE "pseudolife_memory"')
    bank = RedactedUrl(conninfo_with_dbname(url, "pseudolife_memory"))
    with psycopg.connect(bank, autocommit=True, connect_timeout=5) as conn:
        tables = conn.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
                              ).fetchone()[0]
    if tables:
        pytest.fail(f"{ADMIN_ENV} names a server whose pseudolife_memory holds "
                    f"{tables} tables: a real bank. Point it at a disposable server.")
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        _drop_ci_role(conn)  # a run killed before its teardown
    yield url
    # Leave the server as found, but for vector in template1: the login and
    # the empty production-named database this module made.
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        _drop_ci_role(conn)
        if created:
            conn.execute('DROP DATABASE IF EXISTS "pseudolife_memory" WITH (FORCE)')
        if template1_open:
            conn.execute("GRANT CONNECT ON DATABASE template1 TO PUBLIC")


def _drop_ci_role(conn) -> None:
    """Drop this module's role, handing anything it owns (a database a failed
    slice leaked) to the admin first."""
    import psycopg

    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (CI_ROLE,)).fetchone()
    if not exists:
        return
    try:
        conn.execute(f"REASSIGN OWNED BY {CI_ROLE} TO CURRENT_USER")
        conn.execute(f"DROP OWNED BY {CI_ROLE}")
        conn.execute(f"DROP ROLE {CI_ROLE}")
    except psycopg.Error:
        pass  # objects in another database still name it; a disposable server


@pytest.fixture(scope="module")
def provisioned(scratch_admin, tmp_path_factory):
    login_file = tmp_path_factory.mktemp("test-login") / "test-pg.env"
    args = ["create", "--role", CI_ROLE, "--file", str(login_file)]
    out = io.StringIO()
    code = cli.main([*args, "--admin-url", scratch_admin], out=out)
    assert code == cli.EXIT_OK, out.getvalue()
    # Idempotent: a second run re-applies the same password and changes
    # nothing. It gives the admin URL without its password, as the runbook
    # does: libpq reads PGPASSWORD.
    from psycopg.conninfo import conninfo_to_dict

    parts = conninfo_to_dict(scratch_admin)
    host_port = f"{parts.get('host', '127.0.0.1')}:{parts.get('port', '5432')}"
    bare = f"postgresql://{parts.get('user', 'postgres')}@{host_port}/postgres"
    saved = os.environ.get("PGPASSWORD")
    os.environ["PGPASSWORD"] = parts.get("password") or saved or ""
    try:
        again = io.StringIO()
        assert cli.main([*args, "--admin-url", bare], out=again) == cli.EXIT_OK, again.getvalue()
    finally:
        if saved is None:
            os.environ.pop("PGPASSWORD", None)
        else:
            os.environ["PGPASSWORD"] = saved
    assert "re-applied" in again.getvalue()
    assert "already closed to PUBLIC" in again.getvalue()
    from tests.pg_defaults import login_file_credentials

    user, password = login_file_credentials(login_file)
    assert user == CI_ROLE
    return {"admin": scratch_admin, "file": login_file, "user": user,
            "password": password, "host_port": host_port,
            "admin_user": parts.get("user", "postgres")}


def _login_url(provisioned, database):
    from urllib.parse import quote

    return (f"postgresql://{provisioned['user']}:{quote(provisioned['password'], safe='')}"
            f"@{provisioned['host_port']}/{database}")


def test_the_login_cannot_connect_to_the_bank_and_the_owner_still_can(provisioned):
    import psycopg
    from tests.pg_defaults import RedactedUrl, conninfo_with_dbname

    for database in ("pseudolife_memory", "template1"):
        with pytest.raises(psycopg.OperationalError) as refused:
            psycopg.connect(RedactedUrl(_login_url(provisioned, database)),
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


def test_leftovers_of_an_owner_run_are_handed_over_and_refused_meanwhile(provisioned,
                                                                         monkeypatch):
    """A hard-killed run as the admin leaves `pseudolife_memory_test_<pid>`.
    Until `create` hands it over, a run of the login that reuses the name is
    refused with the fix; after it, the login can drop it. Nothing outside
    the pruner's pattern changes owner."""
    import psycopg
    from tests import pg_fixtures
    from tests.pg_defaults import PostgresSetupError, RedactedUrl

    stamp = os.getpid()
    leftover = f"pseudolife_memory_test_{stamp}"
    names = [leftover, f"pseudolife_memory_bench_audit_wsl{stamp}",
             f"pseudolife_memory_testx_{stamp}", "pseudolife_memory_bench"]
    admin = RedactedUrl(provisioned["admin"])
    with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
        bench_existed = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = 'pseudolife_memory_bench'").fetchone()
        for name in names:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            if name != "pseudolife_memory_bench" or not bench_existed:
                conn.execute(f'CREATE DATABASE "{name}"')
    try:
        monkeypatch.setattr(pg_fixtures, "_ensure_state", {})
        monkeypatch.setenv("PSEUDOLIFE_TEST_DATABASE_URL", _login_url(provisioned, leftover))
        monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
        with pytest.raises(PostgresSetupError, match=f"belongs to {provisioned['admin_user']}"):
            pg_fixtures.ensure_test_db()

        out = io.StringIO()
        code = cli.main(["create", "--role", CI_ROLE, "--file", str(provisioned["file"]),
                         "--admin-url", provisioned["admin"]], out=out)
        assert code == cli.EXIT_OK, out.getvalue()
        with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
            owners = dict(conn.execute(
                "SELECT datname, pg_get_userbyid(datdba) FROM pg_database "
                "WHERE datname = ANY(%s)", (names,)).fetchall())
        assert owners[leftover] == CI_ROLE
        assert owners[names[1]] == CI_ROLE
        assert owners[names[2]] != CI_ROLE and owners[names[3]] != CI_ROLE
        for name in names[:2]:
            assert f"database {name}: a leftover test database" in out.getvalue()
        with psycopg.connect(RedactedUrl(_login_url(provisioned, "postgres")),
                             autocommit=True, connect_timeout=5) as conn:
            for name in names[:2]:
                conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    finally:
        with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
            for name in names:
                if name != "pseudolife_memory_bench" or not bench_existed:
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
