"""Throwaway PostgreSQL servers for rows that need a whole server, not a
database: ``test-login create`` runs CREATE/ALTER ROLE, REVOKE CONNECT on
databases and installs an extension in template1, which no shared server may
see.

Two kinds, each fresh per arm so both arms start from identical state:

* ``Cluster``: ``initdb`` from the pg0 bundle in the real home
  (``~/.pg0/installation/<ver>/bin``, which ships pgvector) into a directory
  under the default TEMP, superuser ``postgres`` with a random password,
  SCRAM authentication, listening on 127.0.0.1 only, no Unix socket. One
  template is initialised per process; each arm runs a copy of it on the
  same free port (the arms run one after the other).
* ``Container``: a disposable container from the local ``pseudolife-pg:18``
  image, named ``pl-cf-w1c-testlogin-...``, no published port, removed with
  its anonymous volume afterwards. Never the stack's own container.

Everything started here is stopped and removed at exit, also after a failure.
"""

from __future__ import annotations

import atexit
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

PG0 = Path.home() / ".pg0" / "installation"
ROOT = Path(tempfile.gettempdir()) / f"pl-cf-testlogin-{os.getpid()}"
SUPERUSER = "postgres"
SUPERUSER_PASSWORD = secrets.token_hex(16)
OWNER = "pseudolife"
OWNER_PASSWORD = secrets.token_hex(16)
BANK = "pseudolife_memory"
IMAGE = "pseudolife-pg:18"
CONTAINER_PREFIX = "pl-cf-w1c-testlogin-"
_LIVE_CONTAINER = "pseudolife-mcp-postgres"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = _free_port()
_running: list["Cluster"] = []
_containers: set[str] = set()
_template: Path | None = None
_serial = 0


def _bin(name: str) -> str:
    versions = sorted(p for p in PG0.iterdir() if (p / "bin").is_dir())
    suffix = ".exe" if os.name == "nt" else ""
    return str(versions[-1] / "bin" / (name + suffix))


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    # pg_ctl's server inherits handles: never give it pipes it would hold open.
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, check=True, timeout=180, **kwargs)


def _connect(port: int, dbname: str = "postgres", user: str = SUPERUSER,
             password: str = SUPERUSER_PASSWORD):
    import psycopg  # noqa: PLC0415
    return psycopg.connect(host="127.0.0.1", port=port, dbname=dbname, user=user,
                           password=password, connect_timeout=10, autocommit=True)


class Cluster:
    def __init__(self, data: Path):
        self.data = data
        self.port = PORT

    def start(self) -> None:
        _run([_bin("pg_ctl"), "start", "-w", "-t", "120", "-D", str(self.data),
              "-l", str(self.data.with_suffix(".log"))])
        _running.append(self)

    def stop(self) -> None:
        if self in _running:
            _running.remove(self)
            try:
                _run([_bin("pg_ctl"), "stop", "-w", "-m", "fast", "-D", str(self.data)])
            except (subprocess.SubprocessError, OSError):
                pass
        shutil.rmtree(self.data, ignore_errors=True)
        log = self.data.with_suffix(".log")
        if log.exists():
            log.unlink()

    def connect(self, dbname: str = "postgres", user: str = SUPERUSER,
                password: str = SUPERUSER_PASSWORD):
        return _connect(self.port, dbname, user, password)

    def execute(self, statements: list[str], dbname: str = "postgres") -> None:
        with self.connect(dbname) as conn:
            for statement in statements:
                conn.execute(statement)

    def rows(self, sql: str, dbname: str = "postgres") -> list[list]:
        with self.connect(dbname) as conn:
            return [list(r) for r in conn.execute(sql).fetchall()]

    def can_login(self, user: str, password: str, dbname: str) -> str:
        import psycopg  # noqa: PLC0415
        try:
            with _connect(self.port, dbname, user, password) as conn:
                conn.execute("SELECT 1")
            return "ok"
        except psycopg.Error as exc:
            return getattr(exc, "sqlstate", None) or "refused"


def _build_template() -> Path:
    global _template
    if _template is not None:
        return _template
    ROOT.mkdir(parents=True, exist_ok=True)
    data = ROOT / "template"
    pwfile = ROOT / "pwfile"
    pwfile.write_text(SUPERUSER_PASSWORD + "\n", encoding="ascii")
    try:
        _run([_bin("initdb"), "-D", str(data), "-U", SUPERUSER, f"--pwfile={pwfile}",
              "--auth=scram-sha-256", "-E", "UTF8", "--no-locale", "--no-sync"])
    finally:
        pwfile.unlink()
    with open(data / "postgresql.conf", "a", encoding="utf-8") as conf:
        conf.write(f"\nlisten_addresses = '127.0.0.1'\nport = {PORT}\n"
                   "unix_socket_directories = ''\nfsync = off\nsynchronous_commit = off\n"
                   "full_page_writes = off\nmax_connections = 40\n")
    cluster = Cluster(data)
    cluster.start()
    try:
        # The bank and its owner, as a server elsewhere holds them: the owner
        # is an ordinary login role, the admin URL names the superuser.
        cluster.execute([f"CREATE ROLE {OWNER} LOGIN PASSWORD '{OWNER_PASSWORD}'",
                         f"CREATE DATABASE {BANK} OWNER {OWNER}"])
    finally:
        _running.remove(cluster)
        _run([_bin("pg_ctl"), "stop", "-w", "-m", "fast", "-D", str(data)])
    _template = data
    return data


def fresh() -> Cluster:
    """A started copy of the template, for one arm."""
    global _serial
    template = _build_template()
    _serial += 1
    data = ROOT / f"arm-{_serial}"
    shutil.copytree(template, data)
    cluster = Cluster(data)
    cluster.start()
    return cluster


# ── disposable containers ────────────────────────────────────────────────────

def _docker(*args: str, input: str | None = None, check: bool = True,
            timeout: float = 120) -> subprocess.CompletedProcess:
    if any(_LIVE_CONTAINER in a for a in args):
        raise RuntimeError("refusing to touch the stack's own Postgres container")
    return subprocess.run(["docker", *args], capture_output=True, text=True, input=input,
                          encoding="utf-8", errors="replace", check=check, timeout=timeout)


class Container:
    def __init__(self, name: str, db: str):
        if not name.startswith(CONTAINER_PREFIX):
            raise ValueError(name)
        self.name = name
        self.db = db
        self.password = secrets.token_hex(16)

    def start(self) -> None:
        self.remove()
        _containers.add(self.name)
        _docker("run", "-d", "--name", self.name, "-e", f"POSTGRES_USER={OWNER}",
                "-e", f"POSTGRES_PASSWORD={self.password}", "-e", f"POSTGRES_DB={self.db}",
                IMAGE, "postgres", "-c", "log_statement=all")
        deadline = time.monotonic() + 120
        # TCP answers only once the entrypoint's init server has made way for
        # the final one (the init server listens on the socket alone).
        while time.monotonic() < deadline:
            ready = _docker("exec", self.name, "pg_isready", "-q", "-h", "127.0.0.1",
                            "-U", OWNER, check=False)
            if ready.returncode == 0:
                return
            time.sleep(0.5)
        raise RuntimeError(f"container {self.name} never became ready")

    def remove(self) -> None:
        _docker("rm", "-f", "-v", self.name, check=False)
        _containers.discard(self.name)

    def logs(self) -> bytes:
        """The server's log so far, raw bytes (no newline translation)."""
        if _LIVE_CONTAINER in self.name:
            raise RuntimeError("refusing to touch the stack's own Postgres container")
        proc = subprocess.run(["docker", "logs", self.name], capture_output=True, timeout=60,
                              check=True)
        return proc.stdout + proc.stderr

    def psql(self, sql: str, dbname: str = "postgres") -> str:
        proc = _docker("exec", "-i", self.name, "psql", "-X", "-q", "-tA", "-v",
                       "ON_ERROR_STOP=1", "-U", OWNER, "-d", dbname, input=sql)
        return proc.stdout

    def rows(self, sql: str, dbname: str = "postgres") -> list[list]:
        text = self.psql(f"SELECT coalesce(json_agg(q), '[]') FROM ({sql}) q", dbname)
        # json_agg keeps each row's column order; duplicate column names would
        # collapse, so every query here names distinct columns.
        return [list(row.values()) for row in json.loads(text.strip() or "[]")]

    def can_login(self, user: str, password: str, dbname: str) -> str:
        proc = _docker("exec", "-e", f"PGPASSWORD={password}", self.name, "psql", "-X",
                       "-h", "127.0.0.1", "-U", user, "-d", dbname, "-tA", "-c", "SELECT 1",
                       check=False)
        if proc.returncode == 0:
            return "ok"
        if "permission denied" in proc.stderr:
            return "42501"
        if "password authentication failed" in proc.stderr:
            return "28P01"
        return "refused"


@atexit.register
def _cleanup() -> None:
    for cluster in list(_running):
        cluster.stop()
    for name in list(_containers):
        _docker("rm", "-f", "-v", name, check=False)
    shutil.rmtree(ROOT, ignore_errors=True)
