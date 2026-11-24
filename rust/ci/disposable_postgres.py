"""Throwaway PostgreSQL for CI and harness setup, without the Python package.

Hosted Windows runners cannot run service containers, and the parity and
stress jobs give every row a server of its own, so setup starts one here:
the third-party pg0 wheel (``pg0-embedded``, PostgreSQL 18.1 with pgvector)
initialises and starts a fresh cluster, and the native client's
``test-login create`` gives it the suite's isolated login. Nothing here
imports ``pseudolife_memory``; the pins this needs are in
``rust/ci/requirements-postgres.txt``.

What a server started here guarantees:

* it is owned: a fresh, empty data directory and an instance name nobody
  else uses, so a server that was already running (pg0 attaching instead
  of starting) is refused rather than adopted, and a server pg0 launched
  before a failed check is stopped rather than left running;
* it listens on IPv4 loopback only, and every connection made here goes
  there whatever libpq variables the runner carries;
* its superuser password is not the one pg0 was given on its command line:
  straight after start the role moves to a second random password, sent
  only as a SCRAM verifier, and the first is checked to be refused;
* ``stop`` stops only that server: not one it attached to, and not one that
  replaced it since (a reused pid in a stale postmaster.pid included); a
  server whose identity cannot be confirmed is left running.

pg0's pg_hba.conf keeps its ``password`` rules, so a login sends its
password as text over the loopback interface of a discarded runner.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
from urllib.parse import quote, urlsplit

SUPERUSER = "postgres"
# Variables libpq and the native client read; the native client refuses
# most of them outright, so a stray one in the runner's environment must not
# reach either.
_LIBPQ_PREFIX = "PG"
# TCP on IPv4 loopback only: a dev machine's network must never reach a CI
# cluster, and with no Unix socket a socket lock another account's server
# holds for the same port number (/tmp/.s.PGSQL.<port>.lock, invisible to
# pg0's free-port check) cannot fail the start, as it failed the lite
# bank's on a Linux dev box (rust/shared/embedded_pg.rs, 2026-10-26). Both
# checked against pg0-embedded 0.15.2 in WSL on 2026-11-24. lc_messages=C
# keeps the server's authentication error in the English `_refused` reads.
SERVER_SETTINGS = {"listen_addresses": "127.0.0.1", "unix_socket_directories": "", "lc_messages": "C"}
_AUTH_FAILED = "password authentication failed"
# Below the five-minute timeout of the shortest step that runs lease_ci.py,
# so a hung native client is reported here rather than killed with the step.
TEST_LOGIN_TIMEOUT = 240


class SetupError(RuntimeError):
    """A disposable server could not be started or handed over safely."""


@contextlib.contextmanager
def _without_libpq_environment():
    """Hide the runner's libpq variables (PGHOSTADDR, PGSSLMODE, PGSERVICE,
    ...) from pg0 and from this process's own connections."""
    saved = {key: value for key, value in os.environ.items() if key.upper().startswith(_LIBPQ_PREFIX)}
    for key in saved:
        del os.environ[key]
    try:
        yield
    finally:
        os.environ.update(saved)


def _pidfile(pgdata: Path) -> tuple[int, int] | None:
    """(pid, port) from the cluster's own postmaster.pid, or None."""
    try:
        lines = (pgdata / "postmaster.pid").read_text(encoding="utf-8", errors="replace").splitlines()
        return int(lines[0]), int(lines[3])
    except (OSError, IndexError, ValueError):
        return None


def _connect(port: int, password: str, dbname: str = "postgres"):
    import psycopg  # noqa: PLC0415 - a CI dependency, not the package's
    with _without_libpq_environment():
        return psycopg.connect(host="127.0.0.1", hostaddr="127.0.0.1", port=port, user=SUPERUSER,
                               password=password, dbname=dbname, sslmode="disable", options="",
                               connect_timeout=10, autocommit=True)


def _refused(port: int, password: str) -> bool:
    """True only when the server answers and refuses ``password``."""
    import psycopg  # noqa: PLC0415
    try:
        with _connect(port, password):
            return False
    except psycopg.OperationalError as error:
        return _AUTH_FAILED in str(error)


def _identity(port: int, password: str) -> tuple[str, str] | None:
    """(postmaster start time, data directory) of the server on ``port``
    that accepts ``password``, or None when it cannot be reached."""
    import psycopg  # noqa: PLC0415
    try:
        with _connect(port, password) as conn:
            started, data_directory = conn.execute(
                "SELECT pg_postmaster_start_time()::text, current_setting('data_directory')").fetchone()
            return started, data_directory
    except psycopg.Error:
        return None


def _same_directory(reported: str, pgdata: Path) -> bool:
    try:
        return os.path.samefile(reported, pgdata)
    except OSError:
        return False


def scram_verifier(password: str, salt: bytes | None = None, iterations: int = 4096) -> str:
    """The SCRAM-SHA-256 verifier PostgreSQL stores (RFC 5802/7677), so
    ALTER ROLE never carries the password itself."""
    salt = secrets.token_bytes(16) if salt is None else salt
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda data: base64.b64encode(data).decode("ascii")  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def loopback_host(host: str) -> str:
    """The IPv4 loopback address ``host`` names, or SetupError."""
    host = "127.0.0.1" if host == "localhost" else host.strip("[]")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise SetupError("PostgreSQL did not provide a loopback TCP endpoint") from None
    if address.version != 4 or not address.is_loopback:
        raise SetupError("PostgreSQL did not provide a loopback TCP endpoint")
    return str(address)


class Server:
    """A started cluster; only ``owned`` servers are ever stopped."""

    def __init__(self, *, name: str, data_dir: Path, pgdata: Path, port: int, pid: int,
                 password: str, owned: bool, host: str = "127.0.0.1", started: str = ""):
        self.name, self.data_dir, self.pgdata = name, data_dir, pgdata
        self.host, self.port, self.pid = host, port, pid
        self.password, self.owned, self.started = password, owned, started

    def __repr__(self) -> str:  # never the password
        return f"Server(name={self.name!r}, port={self.port}, pid={self.pid}, owned={self.owned})"

    def admin_conninfo(self, dbname: str = "postgres") -> str:
        """Key/value conninfo without the password (PGPASSWORD carries it)."""
        return f"host={self.host} port={self.port} user={SUPERUSER} dbname={dbname}"

    def url(self, dbname: str) -> str:
        """A superuser URL with the password, for a test runner's environment."""
        return f"postgresql://{SUPERUSER}:{quote(self.password, safe='')}@{self.host}:{self.port}/{dbname}"

    def stop(self) -> bool:
        """Stop and remove the server if this process started it and it is
        still that server. Returns whether it was stopped."""
        if not self.owned:
            return False
        record = _pidfile(self.pgdata)
        if record is None or record[0] != self.pid:
            # Gone, or another server in this data directory since: not ours.
            return False
        # A pid in a stale postmaster.pid can belong to another process by
        # now: the server must still answer with this start time, for this
        # data directory, to our password.
        identity = _identity(self.port, self.password)
        if identity is None or identity[0] != self.started or not _same_directory(identity[1], self.pgdata):
            return False
        _drop(self.name, self.pgdata)
        shutil.rmtree(self.data_dir, ignore_errors=True)
        self.owned = False
        return True


def _drop(name: str, pgdata: Path) -> None:
    import pg0  # noqa: PLC0415
    with _without_libpq_environment():
        pg0.Pg0(name=name, data_dir=str(pgdata)).drop(force=True)


def start(parent: Path, prefix: str) -> Server:
    """A fresh cluster under ``parent``, owned by this process.

    ``parent`` is where initdb may chmod: on hosted Windows the runner's
    staging root (RUNNER_TEMP) carries a group ACL that denies it, so CI
    passes the user profile.
    """
    import pg0  # noqa: PLC0415
    data_dir = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    pgdata = data_dir / "pgdata"
    pgdata.mkdir()
    name = f"{prefix.strip('-_') or 'pl-ci'}-{secrets.token_hex(6)}"
    first = secrets.token_hex(16)
    instance = pg0.Pg0(name=name, data_dir=str(pgdata), username=SUPERUSER, password=first,
                       config=SERVER_SETTINGS)
    try:
        with _without_libpq_environment():
            info = instance.start()
    except pg0.Pg0AlreadyRunningError:
        raise SetupError("PostgreSQL was unexpectedly already running; refusing to adopt it") from None
    except BaseException:
        # pg0 can fail after postgres launched (a readiness timeout): a
        # server in this fresh directory is ours and must not outlive this.
        if _pidfile(pgdata) is not None:
            _drop(name, pgdata)
        raise
    record = _pidfile(pgdata)
    if record is None:
        # pg0 reports a server, but none runs in our fresh directory: it
        # attached to something else, which is not ours to use or stop.
        raise SetupError("PostgreSQL was unexpectedly already running; refusing to adopt it")
    pid, port = record
    try:
        if (info.port and info.port != port) or (info.pid and info.pid != pid):
            raise SetupError("pg0 reported another server than the one in its fresh directory; refusing it")
        # pg0 may omit the URI when its own health probe failed; the server
        # in our directory still answers below or the start fails.
        host = loopback_host(urlsplit(info.uri).hostname or "") if info.uri else "127.0.0.1"
        server = Server(name=name, data_dir=data_dir, pgdata=pgdata, port=port, pid=pid,
                        password=first, owned=True, host=host)
        rotate_password(server)
    except BaseException:
        _drop(name, pgdata)  # ours: our fresh directory's own postmaster
        raise
    return server


def rotate_password(server: Server) -> None:
    """Move the superuser off the password pg0 saw on its command line,
    after checking the server answering is the one in our directory."""
    from psycopg import sql  # noqa: PLC0415
    second = secrets.token_hex(16)
    with _connect(server.port, server.password) as conn:
        started, data_directory = conn.execute(
            "SELECT pg_postmaster_start_time()::text, current_setting('data_directory')").fetchone()
        if not _same_directory(data_directory, server.pgdata):
            raise SetupError("the server on the reported port does not serve the fresh data directory")
        conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(
            sql.Identifier(SUPERUSER), sql.Literal(scram_verifier(second))))
    old, server.password, server.started = server.password, second, started
    if not _refused(server.port, old):
        raise SetupError("PostgreSQL still accepts the start-up password after rotation")
    if _refused(server.port, second):
        raise SetupError("PostgreSQL refuses the rotated password")


def child_environment(server: Server | None = None, base: dict | None = None) -> dict[str, str]:
    """``base`` (default os.environ) without libpq controls or the daemon's
    database URL, plus PGPASSWORD for ``server``."""
    env = {key: value for key, value in (os.environ if base is None else base).items()
           if not key.upper().startswith(_LIBPQ_PREFIX) and key != "PSEUDOLIFE_MCP_DATABASE_URL"}
    if server is not None:
        env["PGPASSWORD"] = server.password
    return env


def create_test_login(binary: Path, server: Server, login_file: Path, *,
                      timeout: float = TEST_LOGIN_TIMEOUT) -> subprocess.CompletedProcess:
    """``test-login create`` through the native client, against ``server``."""
    command = [str(binary), "test-login", "create", "--rotate", "--json", "--file", str(login_file),
               "--admin-url", server.admin_conninfo()]
    return subprocess.run(command, env=child_environment(server), stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def failure_reason(result: subprocess.CompletedProcess) -> str:
    """Why ``test-login create --json`` failed: its report's ``error``
    (printed on stdout), else what it printed on stderr."""
    for line in reversed((result.stdout or "").splitlines()):
        try:
            report = json.loads(line)
        except ValueError:
            continue
        if isinstance(report, dict) and isinstance(report.get("error"), str) and report["error"]:
            return report["error"]
    return (result.stderr or "").strip() or f"no reason printed (exit {result.returncode})"


def native_binary(root: Path) -> Path:
    """The native client a job built or downloaded, at its release path."""
    name = "pseudolife-stdio.exe" if os.name == "nt" else "pseudolife-stdio"
    return root / "rust" / "target" / "release" / name
