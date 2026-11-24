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
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

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
# keeps the server's authentication error in the English `_login` reads.
SERVER_SETTINGS = {"listen_addresses": "127.0.0.1", "unix_socket_directories": "", "lc_messages": "C"}
_AUTH_FAILED = "password authentication failed"
# Below the five-minute timeout of the shortest step that runs lease_ci.py,
# so a hung native client is reported here rather than killed with the step.
TEST_LOGIN_TIMEOUT = 240
# How long a failed start waits for a launched server to answer as ours
# before leaving it running (pg0's own readiness wait failed first).
ABANDON_WAIT = 30
# The one address every connection here pins, and the only one exported.
HOST = "127.0.0.1"
# What pg0's own URI may name for it.
_URI_HOSTS = frozenset({"127.0.0.1", "localhost"})
# How long a postmaster.pid that exists but does not parse yet (the
# postmaster writes it in steps) is re-read before it counts as unreadable.
PIDFILE_SETTLE = 2.0
# The postmaster serves one signal-pipe client at a time; a busy pipe is
# waited for this long before the stop counts as failed.
PIPE_GIVE_UP = 5.0


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


def _pidfile_settled(pgdata: Path) -> tuple[tuple[int, int] | None, bool]:
    """(record, present): the pid file's record, re-read for up to
    PIDFILE_SETTLE seconds while it exists but does not parse yet, and
    whether a pid file is there at all."""
    deadline = time.monotonic() + PIDFILE_SETTLE
    while True:
        record = _pidfile(pgdata)
        present = (pgdata / "postmaster.pid").exists()
        if record is not None or not present or time.monotonic() >= deadline:
            return record, present
        time.sleep(0.1)


def _connect(port: int, password: str, dbname: str = "postgres"):
    import psycopg  # noqa: PLC0415 - a CI dependency, not the package's
    with _without_libpq_environment():
        return psycopg.connect(host=HOST, hostaddr=HOST, port=port, user=SUPERUSER,
                               password=password, dbname=dbname, sslmode="disable", options="",
                               connect_timeout=10, autocommit=True)


def _login(port: int, password: str) -> str:
    """"ok", "refused" (the server's own authentication failure) or
    "unreachable" (anything else: no answer proves nothing)."""
    import psycopg  # noqa: PLC0415
    try:
        with _connect(port, password):
            return "ok"
    except psycopg.OperationalError as error:
        return "refused" if _AUTH_FAILED in str(error) else "unreachable"


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


class Server:
    """A started cluster; only ``owned`` servers are ever stopped."""

    # Every check here connects to HOST, so HOST is all that is exported.
    host = HOST

    def __init__(self, *, name: str, data_dir: Path, pgdata: Path, port: int, pid: int,
                 password: str, owned: bool, started: str = ""):
        self.name, self.data_dir, self.pgdata = name, data_dir, pgdata
        self.port, self.pid = port, pid
        self.password, self.owned, self.started = password, owned, started

    def __repr__(self) -> str:  # never the password
        return f"Server(name={self.name!r}, port={self.port}, pid={self.pid}, owned={self.owned})"

    def admin_conninfo(self, dbname: str = "postgres") -> str:
        """Key/value conninfo without the password (PGPASSWORD carries it)."""
        return f"host={self.host} port={self.port} user={SUPERUSER} dbname={dbname}"

    def stop(self) -> bool:
        """Stop and remove the server if this process started it and it is
        still that server. Returns whether it was stopped."""
        if not self.owned:
            return False
        # The server must still answer with this start time, for this data
        # directory, to our password, and its pid file must still name the
        # pid this process launched (a stale file's pid can be anyone's now).
        if not _stop_verified(self.name, self.pgdata, self.port, self.pid, [self.password], started=self.started):
            return False
        shutil.rmtree(self.data_dir, ignore_errors=True)
        self.owned = False
        return True


# PostgreSQL's fast shutdown is SIGINT; on Windows the postmaster reads its
# signals as one byte on the pipe named for its pid (what pg_ctl's pgkill
# writes), so only a postgres process can be signalled there.
_FAST_SHUTDOWN = 2
STOP_WAIT = 60


def _signal_shutdown(pid: int) -> None:
    """Ask postmaster ``pid`` itself for a fast shutdown. Unlike ``pg_ctl stop
    -D``, which signals whatever pid the file names when it runs, this reaches
    only the process just verified."""
    if os.name == "nt":
        _signal_pipe(pid)
    else:
        import signal  # noqa: PLC0415
        os.kill(pid, signal.SIGINT)


def _windows_api():
    import _winapi  # noqa: PLC0415 - Windows only
    return _winapi


def _signal_pipe(pid: int) -> None:
    """The fast-shutdown byte on postmaster ``pid``'s own signal pipe, as
    pg_ctl's pgkill sends it. CreateFile (not open(), which reports a busy
    pipe as EINVAL with no winerror) says ERROR_PIPE_BUSY while another
    client holds the one listening instance; WaitNamedPipe then waits for it,
    up to PIPE_GIVE_UP seconds in all, and anything else is raised at once."""
    api = _windows_api()
    name = rf"\\.\pipe\pgsignal_{pid}"
    deadline = time.monotonic() + PIPE_GIVE_UP
    while True:
        try:
            handle = api.CreateFile(name, api.GENERIC_READ | api.GENERIC_WRITE, 0, api.NULL,
                                    api.OPEN_EXISTING, 0, api.NULL)
            break
        except OSError as error:
            remaining = deadline - time.monotonic()
            if getattr(error, "winerror", None) != api.ERROR_PIPE_BUSY or remaining <= 0:
                raise
        try:
            api.WaitNamedPipe(name, max(1, int(remaining * 1000)))
        except OSError as error:
            if getattr(error, "winerror", None) != api.ERROR_SEM_TIMEOUT:
                raise
    try:
        api.WriteFile(handle, bytes([_FAST_SHUTDOWN]))
        try:
            api.ReadFile(handle, 1)  # the postmaster's acknowledgement
        except OSError:
            pass  # the byte is delivered; _exited confirms the stop
    finally:
        api.CloseHandle(handle)


def _exited(pgdata: Path, pid: int, wait: float) -> bool:
    """Whether postmaster ``pid`` has left ``pgdata``'s pid file, which it
    removes as the last step of a clean shutdown."""
    deadline = time.monotonic() + wait
    while True:
        if not (pgdata / "postmaster.pid").exists():
            return True
        record = _pidfile(pgdata)
        if record is not None and record[0] != pid:
            return False  # another server holds the directory now
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def _registry_record(name: str) -> Path:
    """pg0's record of instance ``name`` (pg0 keeps ~/.pg0/instances/<name>)."""
    _check_name(name)
    return Path.home() / ".pg0" / "instances" / name


def _check_name(name: str) -> None:
    """An instance name is one path component: pg0 files it by name. A ':'
    is refused on every platform, because on Windows a drive prefix such as
    'C:' resolves the name outside pg0's instances directory."""
    if not name or "/" in name or "\\" in name or ".." in name or ":" in name:
        raise SetupError(f"pg0 instance name {name!r} must be one path component without '..' or ':'; "
                         "pass a plain prefix such as 'pl-ci-postgres-'")


def _deregister(name: str, pgdata: Path) -> None:
    """Remove pg0's registry record of a server already stopped, by hand:
    ``pg0 drop`` would run pg0's own stop by the pid it recorded, which is
    dead by now and may be reused. Only a record naming ``pgdata`` goes."""
    record = _registry_record(name)
    try:
        document = json.loads((record / "instance.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    data_dir = document.get("data_dir") if isinstance(document, dict) else None
    if not isinstance(data_dir, str) or not data_dir:
        return  # not a record this helper wrote: left as it is
    if os.path.normcase(os.path.abspath(data_dir)) == os.path.normcase(os.path.abspath(pgdata)):
        shutil.rmtree(record, ignore_errors=True)


def _stop_verified(name: str, pgdata: Path, port: int, pid: int, passwords: list[str], *,
                   started: str | None = None, wait: float = 0) -> bool:
    """Stop postmaster ``pid`` of ``pgdata`` only once the server on
    ``port`` answers one of ``passwords`` as the server of that directory
    (with ``started`` as its start time, when known) and ``pid`` still holds
    the directory's pid file, and report success only once it has exited.
    Anything unconfirmed is left running."""
    deadline = time.monotonic() + wait
    while True:
        identity = next(filter(None, (_identity(port, password) for password in passwords)), None)
        if identity and _same_directory(identity[1], pgdata) and started in (None, identity[0]):
            break
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)
    # The pid file must still name the pid that just answered (a restart in
    # between is left alone), and only that pid is signalled.
    record = _pidfile(pgdata)
    if record is None or record[0] != pid:
        return False
    try:
        _signal_shutdown(pid)
    except OSError:
        return False
    if not _exited(pgdata, pid, STOP_WAIT):
        return False
    _deregister(name, pgdata)
    return True


def _abandon(name: str, data_dir: Path, pgdata: Path, passwords: list[str]) -> None:
    """Undo a start that failed: stop a server pg0 launched in our fresh
    directory once it answers as ours, then remove the directory once
    nothing runs in it. A server that never answers is left running."""
    record, present = _pidfile_settled(pgdata)
    if present and record is None:
        print(f"disposable PostgreSQL in {pgdata} has a pid file that cannot be read; left as it is", file=sys.stderr)
        return
    if record is not None and not _stop_verified(name, pgdata, record[1], record[0], passwords, wait=ABANDON_WAIT):
        print(f"disposable PostgreSQL in {pgdata} did not answer as ours; left running", file=sys.stderr)
        return
    _deregister(name, pgdata)  # also a record pg0 wrote before failing
    shutil.rmtree(data_dir, ignore_errors=True)


def _scrub(error: BaseException, secrets_: list[str]) -> None:
    """Remove ``secrets_`` from the text of ``error`` and its chain (pg0's
    stderr can carry the start-up password it was given on its argv)."""
    seen, pending = set(), [error]
    while pending:
        error = pending.pop()
        if error is None or id(error) in seen:
            continue
        seen.add(id(error))
        args = list(error.args)
        for index, arg in enumerate(args):
            if isinstance(arg, str):
                for secret in secrets_:
                    arg = arg.replace(secret, "***")
                args[index] = arg
        error.args = tuple(args)
        pending += [error.__cause__, error.__context__]


def start(parent: Path, prefix: str) -> Server:
    """A fresh cluster under ``parent``, owned by this process.

    ``parent`` is where initdb may chmod: on hosted Windows the runner's
    staging root (RUNNER_TEMP) carries a group ACL that denies it, so CI
    passes the user profile. No exception leaves with a password in it.
    """
    first = secrets.token_hex(16)
    holder = []
    try:
        return _start(parent, prefix, first, holder)
    except BaseException as error:
        _scrub(error, [first, *(server.password for server in holder)])
        raise


def _start(parent: Path, prefix: str, first: str, holder: list) -> Server:
    import pg0  # noqa: PLC0415
    name = f"{prefix.strip('-_') or 'pl-ci'}-{secrets.token_hex(6)}"
    _check_name(name)  # before mkdtemp, which takes the prefix as a path part
    data_dir = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    pgdata = data_dir / "pgdata"
    pgdata.mkdir()
    instance = pg0.Pg0(name=name, data_dir=str(pgdata), username=SUPERUSER, password=first,
                       config=SERVER_SETTINGS)
    try:
        with _without_libpq_environment():
            info = instance.start()
    except pg0.Pg0AlreadyRunningError:
        _abandon(name, data_dir, pgdata, [first])
        raise SetupError("PostgreSQL was unexpectedly already running; refusing to adopt it") from None
    except BaseException:
        # pg0 can fail after postgres launched (a readiness timeout): a
        # server in this fresh directory is ours and must not outlive this.
        _abandon(name, data_dir, pgdata, [first])
        raise
    record, present = _pidfile_settled(pgdata)
    if record is None and present:
        _abandon(name, data_dir, pgdata, [first])  # leaves it: something may run there
        raise SetupError(f"PostgreSQL started, but its pid file in {pgdata} cannot be read; "
                         "stop any server on that directory, remove it and retry")
    if record is None:
        # pg0 reports a server, but none runs in our fresh directory: it
        # attached to something else, which is not ours to use or stop.
        _abandon(name, data_dir, pgdata, [first])
        raise SetupError("PostgreSQL was unexpectedly already running; refusing to adopt it")
    pid, port = record
    server = None
    try:
        if (info.port and info.port != port) or (info.pid and info.pid != pid):
            raise SetupError("pg0 reported another server than the one in its fresh directory; refusing it")
        # pg0 may omit the URI when its own health probe failed; the server
        # in our directory still answers below or the start fails. A URI
        # naming any other host than the one every check connects to is
        # refused: only HOST is verified, so only HOST is exported.
        if info.uri and (urlsplit(info.uri).hostname or "") not in _URI_HOSTS:
            raise SetupError(f"pg0 reported a server on another host than {HOST}; refusing it. Install the "
                             "pg0-embedded version rust/ci/requirements-postgres.txt pins and retry")
        server = Server(name=name, data_dir=data_dir, pgdata=pgdata, port=port, pid=pid,
                        password=first, owned=True)
        holder.append(server)
        rotate_password(server)
    except BaseException:
        passwords = [first] if server is None or server.password == first else [server.password, first]
        _abandon(name, data_dir, pgdata, passwords)  # ours once it answers as ours
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
    rotated, previous = _login(server.port, second), _login(server.port, old)
    if rotated != "ok":
        raise SetupError(f"PostgreSQL did not open with the rotated password ({rotated})")
    if previous != "refused":
        raise SetupError("PostgreSQL still accepts the start-up password after rotation" if previous == "ok"
                         else "PostgreSQL could not be asked whether the start-up password still works")


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
    """``test-login create`` through the native client, against ``server``.

    The client runs with an empty profile of its own (HOME, USERPROFILE,
    APPDATA): it refuses an admin URL while a client certificate, key or CRL
    sits in the default PostgreSQL directory those name, and a self-hosted
    runner's own files must not decide a throwaway cluster's setup."""
    command = [str(binary), "test-login", "create", "--rotate", "--json", "--file", str(login_file),
               "--admin-url", server.admin_conninfo()]
    with tempfile.TemporaryDirectory(prefix="pl-test-login-profile-") as profile:
        env = dict(child_environment(server), HOME=profile, USERPROFILE=profile, APPDATA=profile)
        # No console window on Windows (tooling code; its output is captured).
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        return subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, creationflags=flags)


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
