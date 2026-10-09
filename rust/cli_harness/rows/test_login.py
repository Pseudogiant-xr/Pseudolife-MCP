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
import shutil
import subprocess
from pathlib import Path

from .. import normalize
from ..core import WINDOWS, Case
from ..mutants import Mutant
from . import _cluster

ROLE = "pseudolife_test"
SEEDED_PASSWORD = "seeded-" + "s" * 36
# A password shaped like a drawn one, the same in both arms: re-applying it
# must keep it, which the shape alone cannot show.
GENERATED_PASSWORD = secrets.token_urlsafe(32)
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

# Every role (predefined ones included) and every membership: both arms start
# from the same server, so the full sets compare. Columns are named apart, as
# the container's json_agg keys rows by column name.
_ROLES = ("SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin, "
          "rolreplication, rolbypassrls, rolconnlimit, rolvaliduntil::text AS valid_until, "
          "rolconfig FROM pg_roles ORDER BY 1")
_MEMBERS = ("SELECT r.rolname AS granted, m.rolname AS member, g.rolname AS grantor, "
            "a.admin_option, a.inherit_option, a.set_option FROM pg_auth_members a "
            "JOIN pg_roles r ON r.oid = a.roleid JOIN pg_roles m ON m.oid = a.member "
            "JOIN pg_roles g ON g.oid = a.grantor ORDER BY 1, 2, 3")
_DATABASES = ("SELECT datname, pg_get_userbyid(datdba) AS owner, datacl::text AS acl, "
              "datallowconn, datistemplate FROM pg_database ORDER BY 1")
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


def _observe(server, home: Path, login: Path, before: str | None) -> dict:
    """The server and the login file after the run. ``before`` is the file's
    password before it (None without one): ``kept`` says whether the run left
    that same password, compared exactly, so a re-apply that silently draws a
    new password differs even though the new one would validate."""
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
            "kept": None if before is None else password == before,
            "password_sha256": hashlib.sha256(password.encode()).hexdigest(),
        }
    return out


def _password_before(arm, rel: str = LOGIN_REL) -> str | None:
    return _read_login(_login_path(arm, rel)).get(PASSWORD_KEY) or None


@normalize.rule("test-login-password")
def password_rule(obs: dict) -> None:
    """A newly drawn password in the login file becomes ``<validated>``, only
    when this arm's server validated that very password (it authenticates as
    the role and the role's SCRAM verifier verifies it, ``_observe``) and it is
    not the password the file held before the run. A kept, seeded or
    unvalidated password stays as written; ``kept`` itself compares exactly,
    so a re-apply that drew a new password differs from the oracle's."""
    login = (obs.get("db") or {}).get("login")
    if not login:
        return
    digest = login.pop("password_sha256", None)
    verifier = login.get("verifier") or {}
    valid = (login.get("shape") == "token_urlsafe32" and login.get("authenticates") == "ok"
             and login.get("kept") is not True
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


# A SCRAM-SHA-256 verifier for a 16-byte salt: base64 of 16 and of 32 bytes.
_VERIFIER = re.compile(rb"(SCRAM-SHA-256\$[0-9]+:)[A-Za-z0-9+/]{22}==\$[A-Za-z0-9+/]{43}="
                       rb":[A-Za-z0-9+/]{43}=")


@normalize.rule("test-login-verifier-echo")
def verifier_echo_rule(obs: dict) -> None:
    """A failure inside the role change: the server's error CONTEXT quotes the
    statement, including the verifier of a password drawn for this run (its
    salt and keys are random by design, and the password is never kept). Only
    a span with a verifier's exact shape maps, and its iteration count stays."""
    if obs["exit"] != 1:
        return
    stdout = base64.b64decode(obs["stdout"])
    obs["stdout"] = base64.b64encode(_VERIFIER.sub(rb"\1<salt>$<keys>", stdout)).decode()


_NL = b"\r\n" if WINDOWS else b"\n"
_REFUSED = b"test-login: the database refused: "
_DRIVER = "the database refused: <driver-error>"


@normalize.rule("test-login-driver-error")
def driver_error_rule(obs: dict) -> None:
    """On the --admin-url path, the text after ``the database refused: `` is
    the PostgreSQL client library's own error message (psycopg/libpq for the
    oracle, the crate's client for Rust): free. Only that substring of the raw
    stdout is replaced, and only when the bytes around it validate: exit 1;
    text mode, the message is the last one, starting a line, and stdout ends
    with the platform's newline; JSON mode, stdout is exactly one report as
    Python's ``json.dumps`` writes it plus the platform's newline, whose
    ``error`` starts with the prefix. Anything else stays as written."""
    if obs["exit"] != 1:
        return
    stdout = base64.b64decode(obs["stdout"])
    if not stdout.endswith(_NL):
        return
    start = stdout.rfind(_REFUSED)
    if start != -1 and (start == 0 or stdout[:start].endswith(_NL)):
        text = stdout[:start] + _REFUSED + b"<driver-error>" + _NL
    elif stdout.startswith(b"{") and stdout.count(b"\n") == 1:
        try:
            report = json.loads(stdout)
        except ValueError:
            return
        error = report.get("error") if isinstance(report, dict) else None
        if (not isinstance(error, str) or not error.startswith("the database refused: ")
                or (json.dumps(report).encode() + _NL) != stdout):
            return
        encoded = json.dumps(error).encode()
        if stdout.count(encoded) != 1:
            return
        text = stdout.replace(encoded, json.dumps(_DRIVER).encode())
    else:
        return
    obs["stdout"] = base64.b64encode(text).decode()


# ── cases ────────────────────────────────────────────────────────────────────

def _login_path(arm, rel: str = LOGIN_REL) -> Path:
    return arm.home / Path(rel)


def _seed_login(arm, server, *, with_file: bool = True, rel: str = LOGIN_REL,
                provisioned: bool = True, password: str = SEEDED_PASSWORD) -> None:
    """The role as a previous run left it, through the oracle's own statement
    builders and file writer."""
    cli = _oracle()
    statements = cli.role_statements(ROLE, cli.scram_verifier(password), [_cluster.BANK])
    if not provisioned:
        statements = statements[:2] + ["COMMIT"]
    server_exec(server, statements)
    if provisioned:
        server_exec(server, cli.extension_statements()[1:-1], "template1")
    if with_file:
        path = _login_path(arm, rel)
        staged = cli._write_private(path, f"{cli._FILE_HEADER}{USER_KEY}={ROLE}\n"
                                          f"{PASSWORD_KEY}={password}\n")
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
        arm.state["before"] = _password_before(arm, login_rel)

    def after(arm, obs):
        server = arm.state.pop("server")
        held = arm.state.pop("held", None)
        try:
            if held is not None:
                held.close()
            obs["db"] = _observe(server, arm.home, _login_path(arm, login_rel),
                                 arm.state.pop("before"))
        finally:
            server.stop()

    base_env = {"PGPASSWORD": _cluster.SUPERUSER_PASSWORD}
    base_env.update(env or {})
    return Case(id, ["test-login", "create", *argv], env=base_env, setup=setup, after=after,
                rules=("test-login-password", *rules), timeout=120, note=note)


_DOCKER: list[bool] = []


def _no_docker() -> bool:
    """Skip container cases where docker cannot reach an engine (a WSL distro
    without Docker Desktop integration): the --admin-url cases still run."""
    if not _DOCKER:
        import subprocess  # noqa: PLC0415
        try:
            done = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                                  capture_output=True, timeout=30)
            _DOCKER.append(done.returncode == 0)
        except (OSError, subprocess.TimeoutExpired):
            _DOCKER.append(False)
    return not _DOCKER[0]


def _container_case(id: str, argv: list[str], db: str, prepare=None, rules=()) -> Case:
    name = f"{_cluster.CONTAINER_PREFIX}{TOKEN}-{id}"

    def setup(arm):
        container = _cluster.Container(name, db)
        container.start()
        arm.state["server"] = container
        if prepare:
            prepare(arm, container)
        arm.state["before"] = _password_before(arm)
        arm.state["log_offset"] = len(container.logs())

    def after(arm, obs):
        container = arm.state.pop("server")
        try:
            obs["db"] = _observe(container, arm.home, _login_path(arm),
                                 arm.state.pop("before"))
            # The server logs every statement (log_statement=all): whether the
            # CLI's psql scripts arrived with CR line ends, as Python's
            # text-mode stdin writes them on Windows.
            run_log = container.logs()[arm.state.pop("log_offset"):]
            obs["db"]["script_cr"] = b"\r" in run_log
        finally:
            container.remove()

    return Case(id, ["test-login", "create", "--container", name, *argv], setup=setup,
                after=after, rules=("test-login-password", *rules), timeout=180,
                skip_if=_no_docker)


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
    nobin = {"PATH": "{HOME}" + SEP + "nobin"}
    away = ["--container", absent]
    out = [
        # Refusals before any connection. Each still names an absent container
        # and has no docker on PATH: a regression in either arm reaches
        # nothing (the stack's own container is the executor's default).
        Case("role-name-invalid", ["test-login", "create", "--role", "Bad-Name", *away],
             env=nobin),
        Case("role-name-invalid-json", ["test-login", "create", "--role", "9x", "--json", *away],
             env=nobin),
        Case("role-name-empty", ["test-login", "create", "--role", "", *away], env=nobin),
        Case("role-name-unicode", ["test-login", "create", "--role",
                                   "r" + chr(0xF4) + "le" + chr(0x200B), *away], env=nobin),
        Case("bank-not-a-bank", ["test-login", "create", "--bank", "x", "--bank", "postgres",
                                 *away], env=nobin),
        Case("bank-template-json", ["test-login", "create", "--bank", "template1", "--json",
                                    *away], env=nobin),
        Case("no-docker", ["test-login", "create", "--container", absent],
             env={"PATH": "{HOME}" + SEP + "nobin"}),
        Case("no-docker-json", ["test-login", "create", "--json", "--container", absent],
             env={"PATH": "{HOME}" + SEP + "nobin"}),
        # The container path's failure, with no container: docker's own text.
        Case("container-absent", ["test-login", "create", "--container", absent], timeout=60,
             skip_if=_no_docker),
        Case("container-absent-json", ["test-login", "create", "--json", "--container", absent],
             timeout=60, skip_if=_no_docker),
        # The --admin-url path on a throwaway cluster.
        _admin_case("admin-fresh", admin, _shapes),
        _admin_case("admin-fresh-json", [*admin, "--json"], _shapes),
        _admin_case("admin-reapply", admin, lambda arm, s: _seed_login(arm, s)),
        _admin_case("admin-reapply-json", [*admin, "--json"], lambda arm, s: _seed_login(arm, s)),
        _admin_case("admin-rotate", [*admin, "--rotate"], lambda arm, s: _seed_login(arm, s)),
        _admin_case("admin-reapply-generated", admin,
                    lambda arm, s: _seed_login(arm, s, password=GENERATED_PASSWORD)),
        # The verification after the change fails: the owner lost CONNECT.
        _admin_case("admin-after-change-problem", admin,
                    _role(f"REVOKE CONNECT ON DATABASE {_cluster.BANK} FROM {_cluster.OWNER}")),
        # Two refusals at once: the oracle's order decides which one prints.
        _admin_case("admin-two-refusals-owner-daemon", [*admin, "--role", _cluster.OWNER],
                    env={"PSEUDOLIFE_MCP_DATABASE_URL": f"postgresql://{_cluster.OWNER}"
                         f"@127.0.0.1:9/{_cluster.BANK}"}),
        _admin_case("admin-two-refusals-super-connected", admin,
                    _both(_role(f"CREATE ROLE {ROLE} SUPERUSER LOGIN"), peer_locked),
                    hold=hold_locked),
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
        _container_case("container-reapply", [], _cluster.BANK,
                        lambda arm, s: _seed_login(arm, s)),
        _container_case("container-rotate-membership", ["--rotate", "--json"], _cluster.BANK,
                        lambda arm, s: (_seed_login(arm, s, provisioned=False),
                                        server_exec(s, ["CREATE ROLE grp_c",
                                                        f"GRANT grp_c TO {ROLE}"]))),
        # A failure inside the role-change transaction: pg_ names are reserved.
        _container_case("container-role-reserved", ["--role", "pg_test"], _cluster.BANK,
                        rules=("test-login-verifier-echo",)),
    ]
    return [_guard(case) for case in out]


# ── the executor guard ───────────────────────────────────────────────────────

# Cases that name neither --admin-url nor --container: refused before an
# executor is chosen, and run with no docker on PATH. None today; any added
# here must still set PATH to a directory without docker.
_EXECUTOR_FREE: frozenset[str] = frozenset()
_NOBIN = "{HOME}" + SEP + "nobin"


def _guard(case: Case) -> Case:
    """Refuse a case that could reach the stack's own Postgres container.

    Without --admin-url and --container the executor is docker exec into
    ``pseudolife-mcp-postgres``, the live bank's server. Every case names a
    disposable container (the ``pl-cf-w1c-testlogin-`` prefix) or the admin
    URL; one that names neither must be listed in ``_EXECUTOR_FREE``, set PATH
    to a directory without docker, and is checked again at run time."""
    argv = case.argv
    container = argv[argv.index("--container") + 1] if "--container" in argv else None
    if container is not None and (not container.startswith(_cluster.CONTAINER_PREFIX)
                                  or "pseudolife-mcp-postgres" in container):
        raise ValueError(f"{case.id}: --container {container!r} is not a disposable name")
    if "--admin-url" in argv or container is not None:
        return case
    if case.id not in _EXECUTOR_FREE or case.env.get("PATH") != _NOBIN:
        raise ValueError(f"{case.id}: names neither --admin-url nor --container; it would "
                         "reach the stack's own Postgres container")
    inner = case.setup

    def setup(arm):
        search = os.pathsep.join([str(arm.cwd), case.env["PATH"].replace("{HOME}",
                                                                         str(arm.home))])
        if shutil.which("docker", path=search):
            raise RuntimeError(f"{case.id}: docker is reachable; refusing to run")
        if inner:
            inner(arm)

    case.setup = setup
    return case


# ── mutants ──────────────────────────────────────────────────────────────────

_MOD = "shim/src/cli/test_login/mod.rs"
_SQL = "shim/src/cli/test_login/sql.rs"
_FILE = "shim/src/cli/test_login/file.rs"
_EXEC = "shim/src/cli/test_login/exec.rs"

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
    # Review round 1 (2026-10-09).
    Mutant("tl-refusal-order", "test_login", _MOD,
           "if let Some(first) = owned.first().or(owners.first()) {",
           "if let Some(first) = owned.first().or(owners.first())"
           ".filter(|_| daemon_user != Some(role_name)) {",
           ("admin-two-refusals-owner-daemon",)),
    Mutant("tl-scram-iterations", "test_login", _MOD,
           "&scram::verifier(&password, &salt, 4096),", "&scram::verifier(&password, &salt, 4097),",
           ("admin-fresh", "container-reapply")),
    Mutant("tl-script-lf", "test_login", _EXEC,
           "let script = super::super::text_bytes(&script);", "let script = script.into_bytes();",
           ("container-reapply",)),
    Mutant("tl-verify-owner-connect", "test_login", _MOD,
           'if !truthy(info.get("owner_connect")) {',
           'if false && !truthy(info.get("owner_connect")) {', ("admin-after-change-problem",)),
    Mutant("tl-keep-staged-on-failure", "test_login", _MOD,
           "let _ = std::fs::remove_file(&staged);", "let _ = &staged;",
           ("container-role-reserved",)),
    Mutant("tl-grant-predefined", "test_login", _SQL,
           "    statements.push(\"SELECT 'ok'\".into());",
           "    statements.push(\"GRANT pg_read_all_data TO pseudolife\".into());\n"
           "    statements.push(\"SELECT 'ok'\".into());", ("admin-fresh",)),
    Mutant("tl-role-config", "test_login", _SQL,
           "    statements.push(\"SELECT 'ok'\".into());",
           "    statements.push(format!(\"ALTER ROLE {role_ident} SET work_mem = '8MB'\"));\n"
           "    statements.push(\"SELECT 'ok'\".into());", ("admin-fresh",)),
    Mutant("tl-reapply-draws-new", "test_login", _MOD,
           "let password = if reuse {", "let password = if reuse && false {",
           ("admin-reapply-generated",)),
    Mutant("tl-json-compact-colon", "test_login", _MOD,
           'out.push_str(": ");', 'out.push_str(":");', ("admin-bad-password-json",)),
]
