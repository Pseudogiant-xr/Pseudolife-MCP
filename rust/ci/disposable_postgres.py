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
  of starting) is refused rather than adopted;
* it listens on IPv4 loopback only;
* its superuser password is not the one pg0 was given on its command line:
  the role moves to a second random password straight after start, and the
  first one is checked to be refused;
* ``stop`` stops only that server: not one it attached to, and not one
  that replaced it in the data directory since.
"""
from __future__ import annotations

import ipaddress
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


class SetupError(RuntimeError):
    """A disposable server could not be started or handed over safely."""


def _pidfile(pgdata: Path) -> tuple[int, int] | None:
    """(pid, port) from the cluster's own postmaster.pid, or None."""
    try:
        lines = (pgdata / "postmaster.pid").read_text(encoding="utf-8", errors="replace").splitlines()
        return int(lines[0]), int(lines[3])
    except (OSError, IndexError, ValueError):
        return None


def _connect(port: int, password: str, dbname: str = "postgres"):
    import psycopg  # noqa: PLC0415 - a CI dependency, not the package's
    return psycopg.connect(host="127.0.0.1", port=port, user=SUPERUSER, password=password,
                           dbname=dbname, connect_timeout=10, autocommit=True)


def _refused(port: int, password: str) -> bool:
    import psycopg  # noqa: PLC0415
    try:
        with _connect(port, password):
            return False
    except psycopg.OperationalError:
        return True


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
                 password: str, owned: bool, host: str = "127.0.0.1"):
        self.name, self.data_dir, self.pgdata = name, data_dir, pgdata
        self.host, self.port, self.pid = host, port, pid
        self.password, self.owned = password, owned

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
        import pg0  # noqa: PLC0415
        pg0.Pg0(name=self.name, data_dir=str(self.pgdata)).drop(force=True)
        shutil.rmtree(self.data_dir, ignore_errors=True)
        self.owned = False
        return True


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
    # Loopback only: a dev machine's network must never reach a CI cluster.
    instance = pg0.Pg0(name=name, data_dir=str(pgdata), username=SUPERUSER, password=first,
                       config={"listen_addresses": "127.0.0.1"})
    try:
        info = instance.start()
    except pg0.Pg0AlreadyRunningError:
        raise SetupError("PostgreSQL was unexpectedly already running; refusing to adopt it") from None
    record = _pidfile(pgdata)
    if record is None or not info.port or record[1] != info.port or (info.pid and info.pid != record[0]):
        # The server pg0 reports is not one running in our fresh data
        # directory: it attached to something else, which is not ours to use
        # or to stop.
        raise SetupError("PostgreSQL was unexpectedly already running; refusing to adopt it")
    pid, port = record
    try:
        host = loopback_host(urlsplit(info.uri or "").hostname or "")
    except SetupError:
        pg0.Pg0(name=name, data_dir=str(pgdata)).drop(force=True)  # ours: fresh directory, its pid
        raise
    server = Server(name=name, data_dir=data_dir, pgdata=pgdata, port=port, pid=pid,
                    password=first, owned=True, host=host)
    try:
        rotate_password(server)
    except BaseException:
        server.stop()
        raise
    return server


def rotate_password(server: Server) -> None:
    """Move the superuser off the password pg0 saw on its command line."""
    from psycopg import sql  # noqa: PLC0415
    second = secrets.token_hex(16)
    with _connect(server.port, server.password) as conn:
        conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(SUPERUSER), sql.Literal(second)))
    old, server.password = server.password, second
    if not _refused(server.port, old):
        raise SetupError("PostgreSQL still accepts the start-up password after rotation")


def child_environment(server: Server | None = None, base: dict | None = None) -> dict[str, str]:
    """``base`` (default os.environ) without libpq controls or the daemon's
    database URL, plus PGPASSWORD for ``server``."""
    env = {key: value for key, value in (os.environ if base is None else base).items()
           if not key.upper().startswith(_LIBPQ_PREFIX) and key != "PSEUDOLIFE_MCP_DATABASE_URL"}
    if server is not None:
        env["PGPASSWORD"] = server.password
    return env


def create_test_login(binary: Path, server: Server, login_file: Path, *, timeout: float = 300) -> subprocess.CompletedProcess:
    """``test-login create`` through the native client, against ``server``."""
    command = [str(binary), "test-login", "create", "--rotate", "--json", "--file", str(login_file),
               "--admin-url", server.admin_conninfo()]
    return subprocess.run(command, env=child_environment(server), stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def native_binary(root: Path) -> Path:
    """The native client a job built or downloaded, at its release path."""
    name = "pseudolife-stdio.exe" if os.name == "nt" else "pseudolife-stdio"
    return root / "rust" / "target" / "release" / name
