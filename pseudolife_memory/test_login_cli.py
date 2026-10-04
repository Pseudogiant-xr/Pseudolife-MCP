"""``pseudolife-mcp test-login``: a Postgres login for the test suite that
cannot open the bank.

    pseudolife-mcp test-login create [--rotate] [--file PATH] [--role NAME]
                                     [--admin-url URL] [--container NAME]
                                     [--bank DB]... [--json]

The bundled Postgres serves the production bank and the test suite's per-run
databases under one owning role, ``pseudolife``, the server's superuser. Until
this command the suite logged in as that role with ``ops/.env``'s
``POSTGRES_PASSWORD``, so every agent session that ran tests held the bank
owner's password (2026-10-04). ``create`` makes a separate login, idempotently:

* the role (default ``pseudolife_test``): LOGIN and CREATEDB; not superuser,
  CREATEROLE, REPLICATION or BYPASSRLS; any role membership it held is
  revoked. It can create, reset and drop the databases it creates, which is
  everything the suite does on the server.
* ``REVOKE CONNECT ... FROM PUBLIC`` on each production database (default
  ``pseudolife_memory``, the container's ``POSTGRES_DB`` and any ``--bank``),
  and from the role. The bank's owner keeps CONNECT as owner, and is the
  superuser the daemon connects as on the Docker tier. It refuses first
  when a role would lose CONNECT with PUBLIC (not owner, superuser or
  explicitly granted): the user ``PSEUDOLIFE_MCP_DATABASE_URL`` names, when
  this shell has it (else it says the daemon user was not checked), and any
  role connected to a bank now. ``--role`` naming that user is refused.
  The role changes run as one transaction.
* leftover run databases (``LEFTOVER_DATABASE``: the names
  ``tests/pg_fixtures.py`` prunes) handed to the role, so the suite's prune
  can drop what a hard-killed run as the owner left; nothing else.
* ``REVOKE CONNECT ON DATABASE template1 FROM PUBLIC``: a session there
  fails every ``CREATE DATABASE`` that copies it (a restore's, after its
  DROP); copying a template needs no CONNECT on it.
* ``vector`` installed (and updated) in ``template1``. pgvector does not mark
  its extension trusted, so ``CREATE EXTENSION vector`` needs a superuser;
  ``CREATE DATABASE`` copies template1, so every database the login creates
  already has it, and the schema's ``CREATE EXTENSION IF NOT EXISTS`` skips.
* the credentials in a test-only file, owner-only (default
  ``~/.pseudolife-mcp/test-pg.env``, which ``tests/pg_defaults.py`` reads
  before ``ops/.env``): ``PSEUDOLIFE_TEST_PG_USER`` and
  ``PSEUDOLIFE_TEST_PG_PASSWORD``. A second run re-applies the file's
  password, so running suites keep working; ``--rotate`` draws a new one.
  With the role present and no file here holding its password it refuses
  unless ``--rotate``: a new password stops every other copy of the file.

The password is drawn here and never sent: the server gets its SCRAM-SHA-256
verifier, so it is in no statement, log line or process argument.

Where it runs: with ``--admin-url`` (a superuser URL, best without its
password: libpq reads ``PGPASSWORD`` or ``~/.pgpass``), through psycopg; an
error never prints the URL's password.
Otherwise on the Docker host, through ``psql`` in the ``pseudolife-mcp-postgres``
container over its local socket as the container's ``POSTGRES_USER``, as
``ops/restore.sh`` and ``invite --revoke`` do: the host never handles the
owner's password. Without a superuser connection it refuses before any change.

Exit codes: 0 done; 1 the database failed; 2 usage; 4 refused before any change.

Standard library only on the Docker path: on a Docker host ``pseudolife-mcp``
is a shim runtime without the storage layer.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_REFUSED = 4

POSTGRES_CONTAINER = "pseudolife-mcp-postgres"
DEFAULT_ROLE = "pseudolife_test"
# Read by tests/pg_defaults.py (pinned equal by tests/test_test_login_cli.py).
FILE_ENV = "PSEUDOLIFE_TEST_PG_LOGIN_FILE"
DEFAULT_FILE = Path.home() / ".pseudolife-mcp" / "test-pg.env"
USER_KEY = "PSEUDOLIFE_TEST_PG_USER"
PASSWORD_KEY = "PSEUDOLIFE_TEST_PG_PASSWORD"
# The daemon's DSN: its user must keep CONNECT once PUBLIC loses it.
DAEMON_DSN_ENV = "PSEUDOLIFE_MCP_DATABASE_URL"
# storage/schema.py's PRODUCTION_DATABASES, repeated: the Docker path imports
# no storage code (pinned equal by the tests).
DEFAULT_BANKS = ("pseudolife_memory",)
# Every extension ensure_schema creates; none is trusted, so all come from
# template1 (pinned against schema.py by the tests).
EXTENSIONS = ("vector",)
# Never a bank: the admin database and the templates.
_NOT_BANKS = frozenset({"postgres", "template0", "template1"})
# A run's database, as tests/pg_fixtures.py's pruner sees one: the per-run
# test and bench prefixes, the last segment a pid (WSL's carry "wsl"). A
# hard-killed run as the bank owner leaves one the test login cannot drop.
LEFTOVER_DATABASE = re.compile(r"pseudolife_memory_(?:test|bench)_(?:[a-z0-9_]*_)?(?:wsl)?[0-9]+")
# Lower-case, unquoted-identifier shape: no quoting needed in any statement.
_ROLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
DOCKER_TIMEOUT_S = 60
_FILE_HEADER = """\
# Pseudolife-MCP test login, written by `pseudolife-mcp test-login create`.
# The PostgreSQL role the test suite logs in as: it creates and drops its own
# databases and cannot connect to the bank. Not the bank owner's password.
# Rotate with `pseudolife-mcp test-login create --rotate`.
"""


class DatabaseError(RuntimeError):
    """A statement failed; the message carries no password."""


# ── seams (the tests replace these) ──────────────────────────────────────────

def run(cmd: list[str], input: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=DOCKER_TIMEOUT_S,
                              input=input, errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(cmd, 127, "", type(exc).__name__)


class ContainerPsql:
    """``psql`` inside the Postgres container: its local socket, its own
    superuser, no password. SQL goes on stdin, never on a command line."""

    def __init__(self, container: str):
        self.container = container
        self.description = f"the Postgres in container {container}"

    def query(self, database: str | None, statements: list[str]) -> str:
        script = "SET client_min_messages = warning;\n" + "".join(
            f"{statement};\n" for statement in statements)
        # "$1" is the database; empty means the container's POSTGRES_DB.
        cmd = ["docker", "exec", "-i", self.container, "sh", "-c",
               'psql -X -q -tA -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "${1:-$POSTGRES_DB}"',
               "sh", database or ""]
        proc = run(cmd, input=script)
        if proc.returncode != 0:
            raise DatabaseError((proc.stderr or proc.stdout or f"exit {proc.returncode}").strip())
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        return lines[-1].strip() if lines else ""


# A URL's userinfo password, a conninfo ``password=`` value, and the token
# libpq quotes when it cannot percent-decode one: psycopg's error text can
# carry any of them (review, 2026-10-04: a bad --admin-url printed its
# password inside `invalid percent-encoded token: "..."`).
_URL_PASSWORD = re.compile(r"(://[^:/?#@\s]*:)[^@\s]*@")
_KEYWORD_PASSWORD = re.compile(r"(password\s*=\s*)('(?:[^'\\]|\\.)*'|\S+)", re.IGNORECASE)
_QUOTED_TOKEN = re.compile(r'(percent-encoded token:\s*)"[^"]*"', re.IGNORECASE)


def _userinfo_tokens(url: str | None) -> list[str]:
    """The password an admin URL carries, raw and decoded; none when the
    URL has none (libpq then reads PGPASSWORD or ~/.pgpass)."""
    if not url:
        return []
    from urllib.parse import unquote

    found = []
    match = re.match(r"\s*[a-z]+://([^/?#]*)@", url, re.IGNORECASE)
    if match and ":" in match.group(1):
        found.append(match.group(1).split(":", 1)[1])
    match = re.search(r"password\s*=\s*('(?:[^'\\]|\\.)*'|\S+)", url, re.IGNORECASE)
    if match:
        found.append(match.group(1).strip("'"))
    found += [unquote(value) for value in found]
    return [value for value in dict.fromkeys(found) if value]


def redacted(text: str, url: str | None = None) -> str:
    """``text`` with the admin URL's password, and anything shaped like a
    password, replaced by ``***``."""
    for token in sorted(_userinfo_tokens(url), key=len, reverse=True):
        text = text.replace(token, "***")
    text = _URL_PASSWORD.sub(r"\1***@", text)
    text = _KEYWORD_PASSWORD.sub(r"\1***", text)
    return _QUOTED_TOKEN.sub(r'\1"***"', text)


class AdminUrl:
    """psycopg to a superuser URL (``--admin-url``). The URL need not carry a
    password: libpq reads ``PGPASSWORD`` and ``~/.pgpass``."""

    def __init__(self, url: str):
        self._url = url
        self.description = "the Postgres at the admin URL"

    def query(self, database: str | None, statements: list[str]) -> str:
        import psycopg
        from psycopg.conninfo import make_conninfo

        try:
            conninfo = self._url if database is None else make_conninfo(self._url, dbname=database)
            with psycopg.connect(conninfo, autocommit=True, connect_timeout=10) as conn:
                conn.execute("SET client_min_messages = warning")
                result = ""
                for statement in statements:
                    cur = conn.execute(statement)
                    if cur.description:
                        row = cur.fetchone()
                        result = "" if row is None or row[0] is None else str(row[0])
                return result
        except psycopg.Error as exc:
            raise DatabaseError(redacted(f"{type(exc).__name__}: {str(exc).strip()}",
                                             self._url)) from None


# ── SQL ───────────────────────────────────────────────────────────────────────

def scram_verifier(password: str, *, salt: bytes | None = None, iterations: int = 4096) -> str:
    """The SCRAM-SHA-256 verifier PostgreSQL stores for ``password`` (the
    ``PQencryptPasswordConn`` form, RFC 7677). ``ALTER ROLE ... PASSWORD`` takes
    it as is. The password is ASCII, which SASLprep leaves unchanged."""
    salt = os.urandom(16) if salt is None else salt
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()

    def b64(data: bytes) -> str:
        return base64.b64encode(data).decode("ascii")

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _names(values) -> str:
    return ", ".join(_literal(value) for value in values) or "NULL"


def whoami_statement() -> str:
    return ("SELECT json_build_object('who', current_user, 'super', rolsuper, "
            "'db', current_database())::text FROM pg_roles WHERE rolname = current_user")


def _keeps_connect(role_oid: str) -> str:
    """Whether the role row ``r`` keeps CONNECT on the database row ``db``
    once PUBLIC loses it: as a superuser, through the owner role, or through
    an explicit grant to a role it inherits (not PUBLIC, not the test login)."""
    return f"""(r.rolsuper OR pg_has_role(r.oid, db.datdba, 'USAGE') OR EXISTS (
                  SELECT 1 FROM aclexplode(coalesce(db.datacl, acldefault('d', db.datdba))) a
                  WHERE a.privilege_type = 'CONNECT' AND a.grantee <> 0
                    AND a.grantee IS DISTINCT FROM {role_oid}
                    AND pg_has_role(r.oid, a.grantee, 'USAGE')))"""


def _daemon_state(daemon_user: str | None, role_oid: str, banks: list[str]) -> str:
    """Whether the daemon's database user keeps CONNECT on each bank once
    PUBLIC loses it."""
    if not daemon_user:
        return "NULL::json"
    d = _literal(daemon_user)
    return f"""json_build_object(
    'exists', EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {d}),
    'keeps', (SELECT coalesce(json_object_agg(db.datname, {_keeps_connect(role_oid)}), '{{}}'::json)
              FROM pg_database db JOIN pg_roles r ON r.rolname = {d}
              WHERE db.datname IN ({_names(banks)})))"""


def _connected_state(role: str, role_oid: str, banks: list[str]) -> str:
    """Each other role with a client session on a bank now, and whether it
    keeps CONNECT once PUBLIC loses it. A daemon whose database URL is not in
    this shell is usually one of them (review, 2026-10-05)."""
    return f"""(SELECT coalesce(json_agg(json_build_object(
                  'user', r.rolname, 'db', db.datname, 'keeps', {_keeps_connect(role_oid)})
                  ORDER BY db.datname, r.rolname), '[]'::json)
               FROM (SELECT DISTINCT usename, datname FROM pg_stat_activity
                     WHERE backend_type = 'client backend'
                       AND datname IN ({_names(banks)})) s
               JOIN pg_roles r ON r.rolname = s.usename
               JOIN pg_database db ON db.datname = s.datname
               WHERE r.rolname <> {_literal(role)})"""


def state_statement(role: str, banks: list[str], daemon_user: str | None = None) -> str:
    """One JSON row: the role's attributes and memberships, each bank's
    CONNECT for PUBLIC, the role and the owner, the databases the role owns,
    the others PUBLIC may still connect to, and whether the daemon's
    database user, and each role connected to a bank now, keeps CONNECT
    without PUBLIC."""
    r = _literal(role)
    role_oid = f"(SELECT oid FROM pg_roles WHERE rolname = {r})"
    return f"""SELECT json_build_object(
  'daemon', {_daemon_state(daemon_user, role_oid, banks)},
  'connected', {_connected_state(role, role_oid, banks)},
  'template1_public_connect', (SELECT has_database_privilege('public', oid, 'CONNECT')
                               FROM pg_database WHERE datname = 'template1'),
  'role', (SELECT json_build_object('super', rolsuper, 'login', rolcanlogin,
            'createdb', rolcreatedb, 'createrole', rolcreaterole,
            'replication', rolreplication, 'bypassrls', rolbypassrls)
           FROM pg_roles WHERE rolname = {r}),
  'member_of', (SELECT coalesce(json_agg(g.rolname ORDER BY g.rolname), '[]'::json)
                FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
                WHERE m.member = {role_oid}),
  'banks', (SELECT coalesce(json_object_agg(d.datname, json_build_object(
              'owner', pg_get_userbyid(d.datdba),
              'public_connect', has_database_privilege('public', d.oid, 'CONNECT'),
              'role_connect', CASE WHEN {role_oid} IS NULL THEN NULL
                              ELSE has_database_privilege({role_oid}, d.oid, 'CONNECT') END,
              'owner_connect', has_database_privilege(d.datdba, d.oid, 'CONNECT'))), '{{}}'::json)
            FROM pg_database d WHERE d.datname IN ({_names(banks)})),
  'owns', (SELECT coalesce(json_agg(datname ORDER BY datname), '[]'::json)
           FROM pg_database WHERE datdba = {role_oid}),
  'leftovers', (SELECT coalesce(json_agg(d.datname ORDER BY d.datname), '[]'::json)
                FROM pg_database d
                WHERE d.datname ~ '^{LEFTOVER_DATABASE.pattern}$' AND NOT d.datistemplate
                  AND d.datname NOT IN ({_names(banks)})
                  AND d.datdba IS DISTINCT FROM {role_oid}),
  'others', (SELECT coalesce(json_agg(d.datname ORDER BY d.datname), '[]'::json)
             FROM pg_database d
             WHERE d.datallowconn AND NOT d.datistemplate AND d.datname <> 'postgres'
               AND d.datname NOT IN ({_names(banks)})
               AND d.datdba IS DISTINCT FROM {role_oid}
               AND has_database_privilege('public', d.oid, 'CONNECT'))
)::text"""


def role_statements(role: str, verifier: str, banks: list[str],
                    leftovers: list[str] = ()) -> list[str]:
    """Create or reset the role with the verifier as its password, drop every
    role membership it holds, hand it the leftover run databases (so the
    suite's prune can drop them), and close each bank to PUBLIC and to it.
    One transaction: a statement that fails part way leaves the role's old
    password, which the file being replaced still holds."""
    attributes = ("LOGIN CREATEDB NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS "
                  f"INHERIT CONNECTION LIMIT -1 VALID UNTIL 'infinity' PASSWORD {_literal(verifier)}")
    statements = [
        "BEGIN",
        f"""DO $do$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_literal(role)}) THEN
    ALTER ROLE {_ident(role)} WITH {attributes};
  ELSE
    CREATE ROLE {_ident(role)} WITH {attributes};
  END IF;
END $do$""",
        f"""DO $do$ DECLARE grant_row record; BEGIN
  FOR grant_row IN
    SELECT g.rolname AS granted, gr.rolname AS grantor
    FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
    JOIN pg_roles gr ON gr.oid = m.grantor
    WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = {_literal(role)})
  LOOP
    EXECUTE format('REVOKE %I FROM %I GRANTED BY %I', grant_row.granted,
                   {_literal(role)}, grant_row.grantor);
  END LOOP;
END $do$""",
    ]
    for name in leftovers:  # a pruner may drop one meanwhile
        statements.append(f"""DO $do$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_database WHERE datname = {_literal(name)}) THEN
    ALTER DATABASE {_ident(name)} OWNER TO {_ident(role)};
  END IF;
END $do$""")
    for bank in banks:
        statements.append(f"REVOKE CONNECT ON DATABASE {_ident(bank)} FROM PUBLIC")
        statements.append(f"REVOKE ALL ON DATABASE {_ident(bank)} FROM {_ident(role)}")
    # A session in template1 fails every CREATE DATABASE that copies it, the
    # owner's after a restore's DROP included; copying needs no CONNECT.
    statements.append(f"REVOKE CONNECT ON DATABASE {_ident('template1')} FROM PUBLIC")
    statements.append("SELECT 'ok'")
    statements.append("COMMIT")
    return statements


def extension_statements() -> list[str]:
    """For one connection to template1, which it should hold briefly: a
    CREATE DATABASE fails while any other session is connected to its
    template. The last statement answers ``<before>|<after>``."""
    statements = [f"SELECT set_config('pseudolife.extensions_before', ({_extension_versions()}), false)"]
    for name in EXTENSIONS:
        statements += [f"CREATE EXTENSION IF NOT EXISTS {_ident(name)}",
                       f"ALTER EXTENSION {_ident(name)} UPDATE"]
    statements.append(f"SELECT current_setting('pseudolife.extensions_before') || '|' || "
                      f"({_extension_versions()})")
    return statements


def _extension_versions() -> str:
    return ("SELECT coalesce(string_agg(extname || ' ' || extversion, ', ' ORDER BY extname), '') "
            f"FROM pg_extension WHERE extname IN ({_names(EXTENSIONS)})")


# ── the file ──────────────────────────────────────────────────────────────────

def read_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return {}
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _write_private(path: Path, text: str) -> Path:
    """Write ``text`` to a new owner-only file beside ``path``; returns it."""
    from pseudolife_memory.private_state import open_private

    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{os.getpid()}.new")
    staged.unlink(missing_ok=True)
    fd = open_private(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return staged


def _shown(path: Path) -> str:
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


# ── the command ───────────────────────────────────────────────────────────────

class _Report:
    def __init__(self, out, as_json: bool):
        self.out, self.as_json = out, as_json
        self.lines: list[str] = []
        self.data: dict = {}

    def say(self, line: str) -> None:
        self.lines.append(line)
        if not self.as_json:
            print(line, file=self.out, flush=True)

    def finish(self, code: int, error: str | None = None) -> int:
        error = redacted(error) if error else error
        if error and not self.as_json:
            print(f"test-login: {error}", file=self.out, flush=True)
        if self.as_json:
            print(json.dumps({"exit": code, "error": error, "changes": self.lines, **self.data}),
                  file=self.out, flush=True)
        return code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pseudolife-mcp test-login",
        description=("Give the test suite its own Postgres login, which can create and drop "
                     "its own databases and cannot connect to the bank."))
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser(
        "create", help="create the test login, or re-apply it; idempotent",
        description=("Create or reset the test login, close the bank to it (REVOKE CONNECT "
                     "FROM PUBLIC), install vector in template1, and write the login to a "
                     "test-only file. Run on the daemon host, once; again any time."))
    create.add_argument("--role", default=DEFAULT_ROLE, help=f"role name (default {DEFAULT_ROLE})")
    create.add_argument("--file", type=Path, default=None,
                        help=f"where to write the login (default ${FILE_ENV}, else "
                             f"~/.pseudolife-mcp/test-pg.env)")
    create.add_argument("--rotate", action="store_true",
                        help="draw a new password instead of re-applying the file's")
    create.add_argument("--bank", action="append", default=[], metavar="DB",
                        help="another production database to close (repeatable)")
    create.add_argument("--admin-url", default=None,
                        help=("a superuser URL, best without its password (libpq reads "
                              "PGPASSWORD or ~/.pgpass); without it, psql in the Postgres "
                              "container"))
    create.add_argument("--container", default=POSTGRES_CONTAINER,
                        help=f"the Postgres container (default {POSTGRES_CONTAINER})")
    create.add_argument("--json", action="store_true", help="print one JSON report")
    return parser


def main(argv: list[str] | None = None, *, out=None, executor=None) -> int:
    out = sys.stdout if out is None else out
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code == 0 else EXIT_USAGE
    report = _Report(out, args.json)
    if not _ROLE_NAME.fullmatch(args.role):
        return report.finish(EXIT_USAGE, f"role name {args.role!r} must match "
                                         f"{_ROLE_NAME.pattern}")
    named = [bank for bank in args.bank if bank in _NOT_BANKS]
    if named:
        return report.finish(EXIT_USAGE, f"{named[0]} is not a bank and stays open")
    path = args.file or Path(os.environ.get(FILE_ENV) or DEFAULT_FILE)
    banks = list(dict.fromkeys([*DEFAULT_BANKS, *args.bank]))
    daemon = _dsn_parts(os.environ.get(DAEMON_DSN_ENV))
    if executor is None:
        if args.admin_url:
            executor = AdminUrl(args.admin_url)
            if daemon.get("dbname"):
                banks.append(daemon["dbname"])
        elif shutil.which("docker"):
            executor = ContainerPsql(args.container)
        else:
            return report.finish(EXIT_REFUSED, (
                "no superuser connection: run this on the Docker host, where the "
                f"{args.container} container runs, or give --admin-url with a superuser URL"))
    from pseudolife_memory.private_state import PrivateStateError

    try:
        return _create(args, executor, path, banks, report, daemon)
    except (PrivateStateError, OSError) as exc:
        return report.finish(EXIT_FAILED, f"could not write {_shown(path)}: {exc}")
    except DatabaseError as exc:
        return report.finish(EXIT_FAILED, f"the database refused: {exc}")
    except ValueError:  # an answer that is not the JSON the query builds
        return report.finish(EXIT_FAILED, f"{executor.description} gave an answer this "
                                          "command does not understand")


def _dsn_parts(dsn: str | None) -> dict[str, str]:
    """``user`` and ``dbname`` of the daemon's DSN, when it names them; the
    password is never kept. psycopg parses it when installed; on a Docker
    host's shim runtime, the standard library (URL or keyword form)."""
    if not dsn:
        return {}
    try:
        try:
            from psycopg.conninfo import conninfo_to_dict
        except ImportError:
            parts = _dsn_parts_stdlib(dsn)
        else:
            parts = conninfo_to_dict(dsn)
    except Exception:  # noqa: BLE001 - only a hint; --bank names a bank explicitly
        return {}
    return {key: str(parts[key]) for key in ("user", "dbname") if parts.get(key)}


def _dsn_parts_stdlib(dsn: str) -> dict[str, str]:
    from urllib.parse import unquote, urlsplit

    if re.match(r"\s*postgres(?:ql)?://", dsn, re.IGNORECASE):
        url = urlsplit(dsn.strip())
        return {"user": unquote(url.username or ""), "dbname": unquote(url.path.lstrip("/"))}
    return {key: value.strip("'") for key, value in
            re.findall(r"(user|dbname)\s*=\s*('(?:[^'\\]|\\.)*'|\S+)", dsn)}


def _create(args, executor, path: Path, banks: list[str], report: _Report,
            daemon: dict[str, str] | None = None) -> int:
    daemon = daemon or {}
    who = json.loads(executor.query(None, [whoami_statement()]))
    if not who.get("super"):
        return report.finish(EXIT_REFUSED, (
            f"{executor.description} logs in as {who.get('who')!r}, which is not a superuser. "
            "Installing vector into template1 needs one (it is not a trusted extension); on "
            "the Docker tier that is the bank owner, POSTGRES_USER. Nothing was changed."))
    if args.role == who.get("who"):
        return report.finish(EXIT_REFUSED, f"{args.role} is the role this runs as; "
                                           "the test login must be a different one")
    if who.get("db") and who["db"] not in _NOT_BANKS and isinstance(executor, ContainerPsql):
        banks.append(who["db"])  # the container's POSTGRES_DB is the bank
    banks = [bank for bank in dict.fromkeys(banks) if bank not in _NOT_BANKS]
    daemon_user = daemon.get("user")
    before = json.loads(executor.query("postgres", [state_statement(args.role, banks,
                                                                    daemon_user)]))
    role = before.get("role")
    if role and role.get("super"):
        return report.finish(EXIT_REFUSED, f"role {args.role} exists and is a superuser: not "
                                           "a test login, and this will not demote it")
    owned = [bank for bank in before.get("owns") or () if bank in banks]
    owners = [bank for bank, info in (before.get("banks") or {}).items()
              if info.get("owner") == args.role]
    if owned or owners:
        return report.finish(EXIT_REFUSED, f"role {args.role} owns the bank "
                                           f"{(owned or owners)[0]}: not a test login")
    present = sorted((before.get("banks") or {}).keys())
    if daemon_user == args.role:
        return report.finish(EXIT_REFUSED, (
            f"{args.role} is the daemon's database user ({DAEMON_DSN_ENV}): this would close "
            "the bank to the daemon. The test login must be a different role. "
            "Nothing was changed."))
    connected = [entry for entry in before.get("connected") or ()
                 if entry.get("db") in present and entry.get("user") != args.role]
    for entry in connected:
        if not entry.get("keeps"):
            return report.finish(EXIT_REFUSED, (
                f"role {entry['user']} is connected to {entry['db']} now and can connect to it "
                "only through PUBLIC's CONNECT, which this revokes: it would be locked out at "
                "its next connection (a daemon whose database URL is not in this shell, "
                "perhaps). Grant it first, as a superuser: "
                f'GRANT CONNECT ON DATABASE {_ident(entry["db"])} TO {_ident(entry["user"])}; '
                "then run this again. Nothing was changed."))
    daemon_line = None
    if not daemon_user:
        why = ("names no user" if os.environ.get(DAEMON_DSN_ENV)
               else "is not set in this shell")
        daemon_line = (f"  daemon user not checked: {DAEMON_DSN_ENV} {why}; "
                       "only the roles connected to the bank now were")
    else:
        state = before.get("daemon") or {}
        if not state.get("exists"):
            daemon_line = (f"  {DAEMON_DSN_ENV}'s user {daemon_user} is not a role on this "
                           "server: not checked")
        else:
            checked = [bank for bank in present
                       if bank == daemon.get("dbname") or not daemon.get("dbname")]
            locked = [bank for bank in checked if not (state.get("keeps") or {}).get(bank)]
            if locked:
                return report.finish(EXIT_REFUSED, (
                    f"the daemon's database user {daemon_user} ({DAEMON_DSN_ENV}) can connect "
                    f"to {locked[0]} only through PUBLIC's CONNECT, which this revokes: the "
                    "daemon would be locked out of the bank. Grant it first, as a superuser: "
                    f'GRANT CONNECT ON DATABASE {_ident(locked[0])} TO {_ident(daemon_user)}; '
                    "then run this again. Nothing was changed."))
            if checked:
                daemon_line = (f"  daemon user {daemon_user} ({DAEMON_DSN_ENV}): keeps CONNECT "
                               f"on {', '.join(checked)} as owner, superuser or by grant")
    old = read_file(path)
    reusable = old.get(USER_KEY, DEFAULT_ROLE) == args.role and bool(old.get(PASSWORD_KEY))
    if role and not reusable and not args.rotate:
        return report.finish(EXIT_REFUSED, (
            f"role {args.role} already exists and {_shown(path)} holds no password for it, "
            "so this would draw a new one, and every other copy of the login file (another "
            "account's, another machine's) would stop working. Copy the current file here, "
            "or pass --rotate to draw a new password and copy the file again everywhere. "
            "Nothing was changed."))
    reuse = reusable and not args.rotate
    leftovers = [name for name in before.get("leftovers") or ()
                 if LEFTOVER_DATABASE.fullmatch(name) and name not in banks
                 and name not in _NOT_BANKS]
    password = old[PASSWORD_KEY] if reuse else secrets.token_urlsafe(32)
    report.say(f"test-login: on {executor.description}, as {who.get('who')} (superuser)")

    staged = _write_private(path, f"{_FILE_HEADER}{USER_KEY}={args.role}\n"
                                  f"{PASSWORD_KEY}={password}\n")
    try:
        executor.query("postgres", role_statements(args.role, scram_verifier(password), present,
                                                   leftovers))
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    try:
        os.replace(staged, path)  # the role now has this password: so does the file
    except OSError as exc:
        return report.finish(EXIT_FAILED, (
            f"role {args.role} now has the password in {_shown(staged)}, but {_shown(path)} "
            f"could not be replaced ({exc}). Move that file to {_shown(path)}; nothing else "
            "holds this password."))
    how = ("password re-applied from the file" if reuse
           else "password rotated" if role else "password set")
    report.say(f"  role {args.role}: {'reset' if role else 'created'}: LOGIN CREATEDB, not "
               f"superuser, no CREATEROLE, REPLICATION or BYPASSRLS; {how}")
    for granted in before.get("member_of") or ():
        report.say(f"  role {args.role}: membership in {granted} revoked")
    for name in leftovers:
        report.say(f"  database {name}: a leftover test database, handed to {args.role} so "
                   "the suite's prune can drop it")
    for bank in present:
        info = before["banks"][bank]
        state = ("CONNECT revoked from PUBLIC" if info.get("public_connect")
                 else "already closed to PUBLIC")
        report.say(f"  database {bank}: {state}; its owner {info.get('owner')} keeps CONNECT")
    for bank in banks:
        if bank not in present:
            report.say(f"  database {bank}: not on this server")
    if daemon_line:
        report.say(daemon_line)
    for entry in connected:
        report.say(f"  role {entry['user']}, connected to {entry['db']} now, keeps CONNECT as "
                   "owner, superuser or by grant")
    report.say("  template1: " + ("CONNECT revoked from PUBLIC"
                                  if before.get("template1_public_connect")
                                  else "already closed to PUBLIC")
               + "; CREATE DATABASE still copies it")

    old_ext, _, new_ext = executor.query("template1", extension_statements()).partition("|")
    if not old_ext:
        report.say(f"  template1: installed {new_ext}, so every database the test login "
                   "creates has it")
    elif old_ext != new_ext:
        report.say(f"  template1: updated {old_ext} to {new_ext}")
    else:
        report.say(f"  template1: already has {new_ext}")

    after = json.loads(executor.query("postgres", [state_statement(args.role, banks)]))
    problems = _verify(after, args.role, present)
    report.say(f"  wrote {_shown(path)} (owner-only): {USER_KEY}, {PASSWORD_KEY}")
    others = after.get("others") or []
    if others:
        report.say("  other databases PUBLIC may connect to (the test login holds no table "
                   f"privileges there; close them with --bank): {', '.join(others)}")
    report.data = {"role": args.role, "file": str(path), "banks": present,
                   "template1": new_ext, "others": others, "password_reused": reuse}
    if problems:
        return report.finish(EXIT_FAILED, "after the change: " + "; ".join(problems))
    closed = ", ".join(present) or "no production database (none on this server)"
    report.say(f"done: the test suite on this account logs in as {args.role}, which cannot "
               f"connect to {closed}. Copy the file to another account's "
               "~/.pseudolife-mcp/ to give its sessions the same login.")
    return report.finish(EXIT_OK)


def _verify(state: dict, role: str, banks: list[str]) -> list[str]:
    problems = []
    attributes = state.get("role") or {}
    expected = {"super": False, "login": True, "createdb": True, "createrole": False,
                "replication": False, "bypassrls": False}
    wrong = [key for key, value in expected.items() if attributes.get(key) is not value]
    if not attributes:
        problems.append(f"role {role} does not exist")
    elif wrong:
        problems.append(f"role {role} has the wrong {', '.join(wrong)}")
    if state.get("member_of"):
        problems.append(f"role {role} is still a member of {', '.join(state['member_of'])}")
    if state.get("template1_public_connect"):
        problems.append("PUBLIC can still connect to template1")
    for bank in banks:
        info = (state.get("banks") or {}).get(bank) or {}
        if info.get("public_connect"):
            problems.append(f"PUBLIC can still connect to {bank}")
        if info.get("role_connect"):
            problems.append(f"{role} can still connect to {bank}")
        if not info.get("owner_connect"):
            problems.append(f"the owner of {bank} lost CONNECT")
    return problems


if __name__ == "__main__":  # pragma: no cover - `python -m` entry
    sys.exit(main())
