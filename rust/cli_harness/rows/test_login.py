"""CLI-TEST-LOGIN: ``pseudolife-mcp test-login create``.

Canonical argv: ``test-login create`` then ``--rotate``, ``--json``,
``--role R``, ``--file P``, ``--admin-url URL``, ``--container NAME`` (each
at most once, value a separate argument) and ``--bank DB`` (repeatable).
Deferred shapes are covered by the Rust contract tests; this row compares
answered cases only.

Every database case runs against its own throwaway server per arm
(``_cluster``): a fresh copy of one initdb'd cluster for the ``--admin-url``
path, a fresh disposable ``pseudolife-pg:18`` container for the container
path (passed by ``--container``; the stack's own container is never named).
Never the bench server, never the real login file: the file lands in the
arm's disposable home.

Contract per arm: exit code, stdout and stderr byte-exact, every file under
the home, and the server afterwards: roles and their attributes, role
memberships, every database's owner, ACL and flags, template1's extensions,
and the written login validated against that arm's server (the file's
password authenticates as the role and its SCRAM verifier verifies it, the
role cannot connect to the bank) plus the file's owner-only permissions (the
Windows owner and DACL in SDDL with the caller's SID as a token; POSIX mode
and owner). Named rules: ``test-login-password`` (a newly drawn password,
only once validated) and ``test-login-driver-error`` (the PostgreSQL client
library's error text on the --admin-url path).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
from pathlib import Path

from .. import normalize
from ..core import WINDOWS, Case
from ..mutants import Mutant
from . import _cluster

ROLE = "pseudolife_test"
SEEDED_PASSWORD = "seeded-" + "s" * 36
TOKEN = secrets.token_hex(4)
USER_KEY = "PSEUDOLIFE_TEST_PG_USER"
PASSWORD_KEY = "PSEUDOLIFE_TEST_PG_PASSWORD"
LOGIN_REL = ".pseudolife-mcp/test-pg.env"
ADMIN_URL = f"postgresql://{_cluster.SUPERUSER}@127.0.0.1:{_cluster.PORT}/postgres"
SEP = os.sep


def _oracle():
    import pseudolife_memory.test_login_cli as cli  # noqa: PLC0415 (oracle checkout)
    return cli


# ── observation ──────────────────────────────────────────────────────────────

_ROLES = ("SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin, "
          "rolreplication, rolbypassrls, rolconnlimit, rolvaliduntil::text "
          "FROM pg_roles WHERE rolname !~ '^pg_' ORDER BY 1")
_MEMBERS = ("SELECT r.rolname, m.rolname, g.rolname, a.admin_option, a.inherit_option, "
            "a.set_option FROM pg_auth_members a JOIN pg_roles r ON r.oid = a.roleid "
            "JOIN pg_roles m ON m.oid = a.member JOIN pg_roles g ON g.oid = a.grantor "
            "WHERE r.rolname !~ '^pg_' ORDER BY 1, 2, 3")
_DATABASES = ("SELECT datname, pg_get_userbyid(datdba), datacl::text, datallowconn, "
              "datistemplate FROM pg_database ORDER BY 1")
_EXTENSIONS = "SELECT extname, extversion FROM pg_extension ORDER BY 1"


def _verifier_check(verifier: str | None, password: str) -> dict:
    match = re.fullmatch(r"SCRAM-SHA-256\$(\d+):([^$]+)\$([^:]+):(.+)", verifier or "")
    if not match:
        return {"method": None}
    iterations, salt, stored, server = (int(match[1]), base64.b64decode(match[2]),
                                        base64.b64decode(match[3]), base64.b64decode(match[4]))
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    return {"method": "SCRAM-SHA-256", "iterations": iterations, "salt_bytes": len(salt),
            "stored_key": hashlib.sha256(client_key).digest() == stored,
            "server_key": hmac.new(salted, b"Server Key", "sha256").digest() == server}


def _read_login(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    return dict(line.split("=", 1) for line in text.splitlines()
                if "=" in line and not line.startswith("#"))


_ME: str | None = None


def _my_sid() -> str:
    global _ME
    if _ME is None:
        out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True,
                             text=True, check=True).stdout
        _ME = out.strip().rsplit(",", 1)[1].strip('"')
    return _ME


def _sddl(path: Path) -> str:
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    get = advapi.GetNamedSecurityInfoW
    get.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
                    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                    ctypes.POINTER(ctypes.c_void_p)]
    get.restype = wintypes.DWORD
    to_text = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
    to_text.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                        ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p]
    to_text.restype = wintypes.BOOL
    descriptor = ctypes.c_void_p()
    info = 0x1 | 0x4  # OWNER | DACL
    if get(str(path), 1, info, None, None, None, None, ctypes.byref(descriptor)):
        return "unreadable"
    text = wintypes.LPWSTR()
    try:
        if not to_text(descriptor, 1, info, ctypes.byref(text), None):
            return "unconvertible"
        value = text.value
    finally:
        ctypes.windll.kernel32.LocalFree(text)
        ctypes.windll.kernel32.LocalFree(descriptor)
    return value.replace(_my_sid(), "<me>")


def _permissions(path: Path) -> object:
    if not path.exists():
        return None
    if WINDOWS:
        # The effective ACL: owner, whether the DACL is protected from
        # inheritance, and its ACEs. The DACL's auto-inherited control bit
        # (SDDL "AI") grants nothing and is not compared: the oracle's
        # SetFileSecurityW leaves it clear, the crate's SetSecurityInfo sets it.
        sddl = _sddl(path)
        match = re.fullmatch(r"O:([^:]+)D:([A-Z]*)((?:\([^)]*\))*)", sddl)
        if not match:
            return sddl
        return {"owner": match[1], "protected": "P" in match[2].replace("AI", ""),
                "aces": re.findall(r"\([^)]*\)", match[3])}
    info = path.stat()
    return {"mode": oct(info.st_mode & 0o7777), "mine": info.st_uid == os.geteuid()}


def _observe(server, home: Path, login: Path, seeded: bool) -> dict:
    out = {
        "roles": server.rows(_ROLES),
        "members": server.rows(_MEMBERS),
        "databases": server.rows(_DATABASES),
        "template1": server.rows(_EXTENSIONS, "template1"),
        "file_acl": _permissions(login),
        "dir_acl": _permissions(login.parent),
    }
    values = _read_login(login)
    user, password = values.get(USER_KEY), values.get(PASSWORD_KEY)
    if user and password:
        rows = server.rows(f"SELECT rolpassword FROM pg_authid WHERE rolname = '{user}'")
        out["login"] = {
            "user": user,
            "shape": ("seeded" if password == SEEDED_PASSWORD
                      else "token_urlsafe32" if re.fullmatch(r"[A-Za-z0-9_-]{43}", password)
                      else "other"),
            "verifier": _verifier_check(rows[0][0] if rows else None, password),
            "authenticates": server.can_login(user, password, "postgres"),
            "bank": server.can_login(user, password, _cluster.BANK),
            "password_sha256": hashlib.sha256(password.encode()).hexdigest(),
        }
    return out


@normalize.rule("test-login-password")
def password_rule(obs: dict) -> None:
    """A newly drawn password in the login file becomes ``<validated>``, only
    when this arm's server validated that very password: it authenticates as
    the role and the role's SCRAM verifier verifies it (``_observe``). A
    seeded or unvalidated password stays as written."""
    login = (obs.get("db") or {}).get("login")
    if not login:
        return
    digest = login.pop("password_sha256", None)
    verifier = login.get("verifier") or {}
    valid = (login.get("shape") == "token_urlsafe32" and login.get("authenticates") == "ok"
             and verifier.get("stored_key") and verifier.get("server_key"))
    for rel, value in list(obs["files"].items()):
        if not (rel.endswith(".env") and value.startswith("file:")):
            continue
        try:
            lines = base64.b64decode(value[5:]).decode("utf-8").split("\n")
        except UnicodeDecodeError:
            continue  # not a file this run wrote; compared byte for byte
        for index, line in enumerate(lines):
            key, sep, password = line.partition("=")
            if (valid and sep and key == PASSWORD_KEY
                    and hashlib.sha256(password.encode()).hexdigest() == digest):
                lines[index] = f"{PASSWORD_KEY}=<validated>"
        obs["files"][rel] = "file:" + base64.b64encode("\n".join(lines).encode()).decode()


_REFUSED_TEXT = re.compile(rb"(test-login: the database refused: )[^\r\n]*(?:\r?\n[^\r\n]*)*\Z")


@normalize.rule("test-login-driver-error")
def driver_error_rule(obs: dict) -> None:
    """On the --admin-url path, the text after ``the database refused: `` is
    the PostgreSQL client library's own error message (psycopg/libpq for the
    oracle, the crate's client for Rust): free. The exit code (1), the prefix
    and everything printed before it stay exact; only a final ``test-login:
    the database refused:`` message (text) or ``error`` field (JSON) maps."""
    if obs["exit"] != 1:
        return
    stdout = base64.b64decode(obs["stdout"])
    text = _REFUSED_TEXT.sub(rb"\1<driver-error>\r\n" if WINDOWS else rb"\1<driver-error>\n",
                             stdout)
    if text == stdout and stdout.strip().startswith(b"{"):
        report = json.loads(stdout)
        if str(report.get("error") or "").startswith("the database refused: "):
            report["error"] = "the database refused: <driver-error>"
            text = (json.dumps(report) + ("\r\n" if WINDOWS else "\n")).encode()
    obs["stdout"] = base64.b64encode(text).decode()


# ── cases ────────────────────────────────────────────────────────────────────

def _login_path(arm, rel: str = LOGIN_REL) -> Path:
    return arm.home / Path(rel)


def _seed_login(arm, server, *, with_file: bool = True, rel: str = LOGIN_REL,
                provisioned: bool = True) -> None:
    """The role as a previous run left it, through the oracle's own statement
    builders and file writer."""
    cli = _oracle()
    statements = cli.role_statements(ROLE, cli.scram_verifier(SEEDED_PASSWORD), [_cluster.BANK])
    if not provisioned:
        statements = statements[:2] + ["COMMIT"]
    server_exec(server, statements)
    if provisioned:
        server_exec(server, cli.extension_statements()[1:-1], "template1")
    if with_file:
        path = _login_path(arm, rel)
        staged = cli._write_private(path, f"{cli._FILE_HEADER}{USER_KEY}={ROLE}\n"
                                          f"{PASSWORD_KEY}={SEEDED_PASSWORD}\n")
        os.replace(staged, path)


def _hand_edited(body: str | bytes):
    """A login file someone edited by hand (BOM, CRLF, quotes, spacing, bad
    bytes) for an existing role: the reader, not the writer, is under test."""
    def prepare(arm, server):
        _seed_login(arm, server, with_file=False)
        path = _login_path(arm)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
    return prepare


def server_exec(server, statements: list[str], dbname: str = "postgres") -> None:
    if isinstance(server, _cluster.Cluster):
        server.execute(statements, dbname)
    else:
        server.psql("".join(f"{s};\n" for s in statements), dbname)


def _admin_case(id: str, argv: list[str], prepare=None, *, env=None, rules=(),
                login_rel: str = LOGIN_REL, hold=None, note: str = "") -> Case:
    def setup(arm):
        server = _cluster.fresh()
        arm.state["server"] = server
        if prepare:
            prepare(arm, server)
        if hold:
            arm.state["held"] = hold(server)

    def after(arm, obs):
        server = arm.state.pop("server")
        held = arm.state.pop("held", None)
        try:
            if held is not None:
                held.close()
            obs["db"] = _observe(server, arm.home, _login_path(arm, login_rel), False)
        finally:
            server.stop()

    base_env = {"PGPASSWORD": _cluster.SUPERUSER_PASSWORD}
    base_env.update(env or {})
    return Case(id, ["test-login", "create", *argv], env=base_env, setup=setup, after=after,
                rules=("test-login-password", *rules), timeout=120, note=note)


def _container_case(id: str, argv: list[str], db: str) -> Case:
    name = f"{_cluster.CONTAINER_PREFIX}{TOKEN}-{id}"

    def setup(arm):
        container = _cluster.Container(name, db)
        container.start()
        arm.state["server"] = container

    def after(arm, obs):
        container = arm.state.pop("server")
        try:
            obs["db"] = _observe(container, arm.home, _login_path(arm), False)
        finally:
            container.remove()

    return Case(id, ["test-login", "create", "--container", name, *argv], setup=setup,
                after=after, rules=("test-login-password",), timeout=180)


def _shapes(arm, server) -> None:
    """Leftover run databases (handed over), a database that only looks like
    one, another database PUBLIC can open, all owned by the bank owner."""
    server.execute([f"CREATE DATABASE pseudolife_memory_test_4242 OWNER {_cluster.OWNER}",
                    f"CREATE DATABASE pseudolife_memory_bench_cpu_wsl77 OWNER {_cluster.OWNER}",
                    f"CREATE DATABASE pseudolife_memory_testx OWNER {_cluster.OWNER}",
                    f"CREATE DATABASE pl_other OWNER {_cluster.OWNER}"])


def _role(sql: str):
    def prepare(arm, server):
        server.execute([sql])
    return prepare


def _hold_peer(grant: bool):
    def prepare(arm, server):
        statements = ["CREATE ROLE peer LOGIN PASSWORD 'peer-password'"]
        if grant:
            statements.append(f"GRANT CONNECT ON DATABASE {_cluster.BANK} TO peer")
        server.execute(statements)

    def hold(server):
        return server.connect(_cluster.BANK, "peer", "peer-password")

    return prepare, hold


def _both(*prepares):
    def prepare(arm, server):
        for step in prepares:
            step(arm, server)
    return prepare


def cases() -> list[Case]:
    admin = ["--admin-url", ADMIN_URL]
    owner_url = f"postgresql://{_cluster.OWNER}@127.0.0.1:{_cluster.PORT}/postgres"
    daemon = "postgresql://{}127.0.0.1:9/{}"
    absent = f"{_cluster.CONTAINER_PREFIX}absent-{TOKEN}"
    peer_locked, hold_locked = _hold_peer(False)
    peer_keeps, hold_keeps = _hold_peer(True)
    out = [
        # Refusals before any connection.
        Case("role-name-invalid", ["test-login", "create", "--role", "Bad-Name"]),
        Case("role-name-invalid-json", ["test-login", "create", "--role", "9x", "--json"]),
        Case("role-name-empty", ["test-login", "create", "--role", ""]),
        Case("role-name-unicode", ["test-login", "create", "--role", "r" + chr(0xF4) + "le" + chr(0x200B)]),
        Case("bank-not-a-bank", ["test-login", "create", "--bank", "x", "--bank", "postgres"]),
        Case("bank-template-json", ["test-login", "create", "--bank", "template1", "--json"]),
        Case("no-docker", ["test-login", "create", "--container", absent],
             env={"PATH": "{HOME}" + SEP + "nobin"}),
        Case("no-docker-json", ["test-login", "create", "--json", "--container", absent],
             env={"PATH": "{HOME}" + SEP + "nobin"}),
        # The container path's failure, with no container: docker's own text.
        Case("container-absent", ["test-login", "create", "--container", absent], timeout=60),
        Case("container-absent-json", ["test-login", "create", "--json", "--container", absent],
             timeout=60),
        # The --admin-url path on a throwaway cluster.
        _admin_case("admin-fresh", admin, _shapes),
        _admin_case("admin-fresh-json", [*admin, "--json"], _shapes),
        _admin_case("admin-reapply", admin, lambda arm, s: _seed_login(arm, s)),
        _admin_case("admin-reapply-json", [*admin, "--json"], lambda arm, s: _seed_login(arm, s)),
        _admin_case("admin-rotate", [*admin, "--rotate"], lambda arm, s: _seed_login(arm, s)),
        _admin_case("admin-reapply-hand-edited", admin, _hand_edited(
            chr(0xFEFF) + f"PSEUDOLIFE_TEST_PG_PASSWORD=\"{SEEDED_PASSWORD}\"  \r\n# edited\r\n"
            "  PSEUDOLIFE_TEST_PG_USER = 'pseudolife_test'\r\nnot a pair\r\n")),
        _admin_case("admin-file-empty-password", admin, _hand_edited(
            "PSEUDOLIFE_TEST_PG_USER=pseudolife_test\nPSEUDOLIFE_TEST_PG_PASSWORD=''\n")),
        _admin_case("admin-file-not-utf8", admin, _hand_edited(
            b"PSEUDOLIFE_TEST_PG_PASSWORD=" + bytes([0xFF]) + SEEDED_PASSWORD.encode() + b"\n")),
        _admin_case("admin-custom-role", [*admin, "--role", "ci_login", "--rotate"], _shapes),
        _admin_case("admin-exists-no-file", admin,
                    lambda arm, s: _seed_login(arm, s, with_file=False)),
        _admin_case("admin-exists-no-file-rotate", [*admin, "--rotate"],
                    lambda arm, s: _seed_login(arm, s, with_file=False)),
        _admin_case("admin-file-other-role", [*admin, "--role", "other_login"],
                    lambda arm, s: (_seed_login(arm, s),
                                    s.execute(["CREATE ROLE other_login LOGIN"]))),
        _admin_case("admin-membership-revoked", admin,
                    _both(lambda arm, s: _seed_login(arm, s, provisioned=False),
                          _role(f"CREATE ROLE grp_a; CREATE ROLE grp_b; "
                                f"GRANT grp_b TO {ROLE}; GRANT grp_a TO {ROLE}"))),
        _admin_case("admin-superuser-role", admin, _role(f"CREATE ROLE {ROLE} SUPERUSER LOGIN")),
        _admin_case("admin-role-owns-bank", [*admin, "--role", _cluster.OWNER]),
        _admin_case("admin-role-is-self", [*admin, "--role", _cluster.SUPERUSER]),
        _admin_case("admin-not-superuser", ["--admin-url", owner_url],
                    env={"PGPASSWORD": _cluster.OWNER_PASSWORD}),
        _admin_case("admin-not-superuser-json", ["--admin-url", owner_url, "--json"],
                    env={"PGPASSWORD": _cluster.OWNER_PASSWORD}),
        _admin_case("admin-extra-banks", [*admin, "--bank", "pl_extra", "--bank", "pl_missing",
                                          "--bank", "pl_extra"],
                    _role(f"CREATE DATABASE pl_extra OWNER {_cluster.OWNER}")),
        _admin_case("admin-extra-banks-json", [*admin, "--json", "--bank", "pl_extra",
                                               "--bank", "aaa_bank"],
                    _both(_role(f"CREATE DATABASE pl_extra OWNER {_cluster.OWNER}"),
                          _role(f"CREATE DATABASE aaa_bank OWNER {_cluster.OWNER}"))),
        _admin_case("admin-daemon-keeps", admin,
                    env={"PSEUDOLIFE_MCP_DATABASE_URL": daemon.format("pseudolife:x@",
                                                                      _cluster.BANK)}),
        _admin_case("admin-daemon-locked", admin, _role("CREATE ROLE daemonuser LOGIN"),
                    env={"PSEUDOLIFE_MCP_DATABASE_URL": daemon.format("daemonuser@",
                                                                      _cluster.BANK)}),
        _admin_case("admin-daemon-granted", admin,
                    _role("CREATE ROLE daemonuser LOGIN; "
                          f"GRANT CONNECT ON DATABASE {_cluster.BANK} TO daemonuser"),
                    env={"PSEUDOLIFE_MCP_DATABASE_URL": daemon.format("daemonuser@",
                                                                      _cluster.BANK)}),
        _admin_case("admin-daemon-not-a-role", admin,
                    env={"PSEUDOLIFE_MCP_DATABASE_URL": daemon.format("ghost@",
                                                                      _cluster.BANK)}),
        _admin_case("admin-daemon-no-user", admin,
                    env={"PSEUDOLIFE_MCP_DATABASE_URL": daemon.format("", "pl_daemon_db")}),
        _admin_case("admin-daemon-is-role", admin,
                    env={"PSEUDOLIFE_MCP_DATABASE_URL": daemon.format(f"{ROLE}:pw@",
                                                                      _cluster.BANK)}),
        _admin_case("admin-daemon-empty", admin, env={"PSEUDOLIFE_MCP_DATABASE_URL": ""}),
        _admin_case("admin-connected-locked", admin, peer_locked, hold=hold_locked),
        _admin_case("admin-connected-keeps", admin, peer_keeps, hold=hold_keeps),
        _admin_case("admin-file-env", admin,
                    env={"PSEUDOLIFE_TEST_PG_LOGIN_FILE": "{HOME}" + SEP + "alt" + SEP
                         + "login.env"},
                    login_rel="alt/login.env"),
        _admin_case("admin-file-relative", [*admin, "--file", "rel.env"],
                    login_rel="cwd/rel.env"),
        _admin_case("admin-file-option", [*admin, "--file", "{HOME}" + SEP + "x" + SEP + "y.env"],
                    login_rel="x/y.env"),
        _admin_case("admin-bad-password", admin, env={"PGPASSWORD": "wrong"},
                    rules=("test-login-driver-error",)),
        _admin_case("admin-bad-password-json", [*admin, "--json"], env={"PGPASSWORD": "wrong"},
                    rules=("test-login-driver-error",)),
        # The container path on disposable containers.
        _container_case("container-fresh", [], _cluster.BANK),
        _container_case("container-other-db-json", ["--json"], "pl_bank2"),
    ]
    return out


# ── mutants ──────────────────────────────────────────────────────────────────

_MOD = "shim/src/cli/test_login/mod.rs"
_SQL = "shim/src/cli/test_login/sql.rs"
_FILE = "shim/src/cli/test_login/file.rs"

MUTANTS = [
    Mutant("tl-skip-template1-revoke", "test_login", _SQL,
           '    statements.push(format!(\n        "REVOKE CONNECT ON DATABASE {} FROM PUBLIC",\n'
           '        ident("template1")\n    ));\n',
           "", ("admin-fresh",)),
    Mutant("tl-refused-exit-3", "test_login", _MOD,
           "const EXIT_REFUSED: u8 = 4;", "const EXIT_REFUSED: u8 = 3;",
           ("admin-exists-no-file", "no-docker")),
    Mutant("tl-closed-token", "test_login", _MOD,
           '"already closed to PUBLIC"\n        };', '"already closed to public"\n        };',
           ("admin-reapply",)),
    Mutant("tl-skip-file-replace", "test_login", _MOD,
           "if let Err(error) = std::fs::rename(&staged, path) {",
           "if let Err(error) = Ok::<(), std::io::Error>(()) {", ("admin-fresh",)),
    Mutant("tl-banks-reverse", "test_login", _MOD,
           "    present.sort();\n", "    present.sort();\n    present.reverse();\n",
           ("admin-extra-banks-json",)),
    Mutant("tl-skip-leftovers", "test_login", _MOD,
           "LEFTOVER.is_match(name) && !banks", "!LEFTOVER.is_match(name) && !banks",
           ("admin-fresh",)),
    Mutant("tl-reuse-flag", "test_login", _MOD,
           '("password_reused", Value::Bool(reuse))', '("password_reused", Value::Bool(!reuse))',
           ("admin-reapply-json",)),
    Mutant("tl-file-not-private", "test_login", _FILE,
           "    let mut file = open_private(&staged)?;",
           "    let mut file = fs::File::create(&staged).map_err(|e| e.to_string())?;",
           ("admin-fresh",)),
    Mutant("tl-no-bom-strip", "test_login", _FILE,
           'let data = data.strip_prefix(b"\\xef\\xbb\\xbf").unwrap_or(&data);',
           "let data = &data[..];", ("admin-reapply-hand-edited",)),
    Mutant("tl-container-db-ignored", "test_login", _MOD,
           "        && executor.is_container()\n", "        && !executor.is_container()\n",
           ("container-other-db-json",)),
]
