"""The CI PostgreSQL helper and the lease row's fixture, with pg0, the
server and the native client faked: nothing here starts a process."""
from __future__ import annotations

import base64
import contextlib
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
import types
from unittest.mock import patch

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rust.ci import disposable_postgres as postgres  # noqa: E402

START_TIME = "2026-11-24 04:40:54.449893+00"


class _AlreadyRunning(Exception):
    pass


class _Pg0Error(Exception):
    pass


class FakePg0:
    """The pg0 module: ``start`` writes postmaster.pid as a server would."""

    def __init__(self, server):
        self.server = server
        self.pid, self.port, self.uri_host = 4242, 55432, "localhost"
        self.already_running = self.fail_after_launch = False
        self.pidfile = self.answers = True
        self.info_pid = self.info_port = None
        self.instances = []
        self.Pg0AlreadyRunningError = _AlreadyRunning
        fake = self

        class Pg0:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                fake.instances.append(kwargs)

            def start(self):
                fake.env_during_start = dict(os.environ)
                if fake.already_running:
                    raise _AlreadyRunning("instance already running")
                data_dir = Path(self.kwargs["data_dir"])
                record = Path.home() / ".pg0" / "instances" / self.kwargs["name"]
                record.mkdir(parents=True)
                (record / "instance.json").write_text(json.dumps({"data_dir": str(data_dir), "pid": fake.pid}),
                                                      encoding="utf-8")
                if fake.pidfile:
                    (data_dir / "postmaster.pid").write_text(
                        f"{fake.pid}\n{data_dir}\n0\n{fake.port}\n", encoding="utf-8")
                    if fake.answers:
                        fake.server.boot(data_dir, self.kwargs["password"])
                if fake.fail_after_launch:
                    raise _Pg0Error("timed out waiting for the server")
                port = fake.port if fake.info_port is None else fake.info_port
                return types.SimpleNamespace(
                    pid=fake.pid if fake.info_pid is None else fake.info_pid, port=port,
                    uri=f"postgresql://postgres:{self.kwargs['password']}@{fake.uri_host}:{port}/postgres")

            def drop(self, force=True):
                # pg0 drop stops by the pid pg0 recorded, which may be dead and
                # reused by now: the helper must never call it.
                raise AssertionError("pg0 drop was called")

        self.Pg0 = Pg0


class FakeServer:
    """The server pg0 started: which passwords it accepts and what it says
    about itself."""

    def __init__(self):
        self.running = False
        self.data_directory = None
        self.started = START_TIME
        self.honour_alter = True
        self.down_after_alter = False
        self.keep_old = False
        self.plain, self.verifier = None, None
        self.statements = []

    def boot(self, data_dir, password):
        self.running, self.data_directory, self.plain = True, str(data_dir), password

    def accepts(self, password):
        if self.verifier is not None:
            match = re.fullmatch(r"SCRAM-SHA-256\$(\d+):([^$]+)\$.+", self.verifier)
            salt = base64.b64decode(match.group(2))
            if postgres.scram_verifier(password, salt, int(match.group(1))) == self.verifier:
                return True
            return self.keep_old and password == self.plain
        return password == self.plain

    def connect(self, port, password, dbname="postgres"):
        if not self.running:
            raise psycopg.OperationalError("connection failed: Connection refused")
        if not self.accepts(password):
            raise psycopg.OperationalError('FATAL:  password authentication failed for user "postgres"')
        server = self

        class Connection:
            def execute(self, statement):
                text = statement if isinstance(statement, str) else repr(statement)
                server.statements.append(text)
                match = re.search(r"Literal\('(SCRAM-SHA-256[^']+)'\)", text)
                if match and server.honour_alter:
                    server.verifier = match.group(1)
                if match and server.down_after_alter:
                    server.running = False
                return types.SimpleNamespace(fetchone=lambda: (server.started, server.data_directory))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return Connection()


class FakeWinApi:
    """The _winapi calls the Windows signal pipe makes, on a fake clock:
    ``busy`` CreateFile attempts fail with ERROR_PIPE_BUSY first, and each
    WaitNamedPipe advances the clock by ``wait_takes`` seconds."""
    GENERIC_READ, GENERIC_WRITE, OPEN_EXISTING, NULL = 0x80000000, 0x40000000, 3, 0
    ERROR_PIPE_BUSY, ERROR_SEM_TIMEOUT = 231, 121

    def __init__(self, monkeypatch, *, busy=0, wait_takes=0.0, wait_error=None, create_error=None,
                 read_error=None):
        self.busy, self.wait_takes, self.wait_error = busy, wait_takes, wait_error
        self.create_error, self.read_error = create_error, read_error
        self.calls, self.now = [], 1000.0
        monkeypatch.setattr(postgres, "_windows_api", lambda: self)
        monkeypatch.setattr(postgres.time, "monotonic", lambda: self.now)

    @staticmethod
    def _error(winerror, text):
        error = OSError(text)
        error.winerror = winerror
        return error

    def CreateFile(self, name, access, share, security, disposition, flags, template):
        self.calls.append(("CreateFile", name, access, disposition))
        if self.create_error is not None:
            raise self.create_error
        if self.busy:
            self.busy -= 1
            raise self._error(self.ERROR_PIPE_BUSY, "All pipe instances are busy")
        return "handle"

    def WaitNamedPipe(self, name, timeout_ms):
        self.calls.append(("WaitNamedPipe", name, timeout_ms))
        self.now += self.wait_takes
        if self.wait_error is not None:
            raise self._error(self.wait_error, "wait failed")

    def WriteFile(self, handle, data):
        self.calls.append(("WriteFile", handle, data))
        return len(data), 0

    def ReadFile(self, handle, size):
        self.calls.append(("ReadFile", handle, size))
        if self.read_error is not None:
            raise self.read_error
        return b"\x02", 0

    def CloseHandle(self, handle):
        self.calls.append(("CloseHandle", handle))

    def names(self):
        return [call[0] for call in self.calls]


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    server = FakeServer()
    pg0 = FakePg0(server)
    pg0.Pg0Error = _Pg0Error
    monkeypatch.setitem(sys.modules, "pg0", pg0)
    monkeypatch.setattr(postgres, "_connect", server.connect)
    monkeypatch.setattr(postgres, "ABANDON_WAIT", 0)
    monkeypatch.setattr(postgres, "STOP_WAIT", 0)
    monkeypatch.setattr(postgres, "PIDFILE_SETTLE", 0)
    home = tmp_path / "home"
    home.mkdir()
    parent = tmp_path / "parent"
    parent.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    deregistered = []
    real_deregister = postgres._deregister

    def deregister(name, pgdata):
        had = postgres._registry_record(name).exists()
        real_deregister(name, pgdata)
        if had and not postgres._registry_record(name).exists():
            deregistered.append(name)

    monkeypatch.setattr(postgres, "_deregister", deregister)
    pg0.drops = deregistered  # what the old pg0 drop calls recorded: a record removed
    stops = []

    def signal_shutdown(pid):
        # The postmaster exits and removes its pid file.
        stops.append(pid)
        for pidfile in tmp_path.rglob("postmaster.pid"):
            if pidfile.read_text(encoding="utf-8").splitlines()[0] == str(pid):
                pidfile.unlink()
        server.running = False

    monkeypatch.setattr(postgres, "_signal_shutdown", signal_shutdown)
    return types.SimpleNamespace(pg0=pg0, server=server, parent=parent, stops=stops, home=home)


def test_start_owns_a_fresh_loopback_cluster(fakes):
    server = postgres.start(fakes.parent, "pl-test-")
    assert server.owned and server.port == 55432 and server.pid == 4242 and server.host == "127.0.0.1"
    started = fakes.pg0.instances[0]
    assert started["config"] == {"listen_addresses": "127.0.0.1", "unix_socket_directories": "",
                                 "lc_messages": "C"}
    assert Path(started["data_dir"]).parent.parent == fakes.parent
    assert server.admin_conninfo() == "host=127.0.0.1 port=55432 user=postgres dbname=postgres"
    assert server.password not in server.admin_conninfo() and server.password not in repr(server)


@pytest.mark.parametrize("prefix", ["../pl-", "pl/../", "a/b-", "a\\b-", "pl..x-"])
def test_start_refuses_a_prefix_that_is_not_one_path_component(fakes, prefix):
    with pytest.raises(postgres.SetupError, match="one path component"):
        postgres.start(fakes.parent, prefix)
    # nothing was made, in the parent or beside it (iterdir's order is the platform's)
    assert list(fakes.parent.iterdir()) == [] and set(fakes.parent.parent.iterdir()) == {
        fakes.home, fakes.parent} and fakes.pg0.instances == []


@pytest.mark.parametrize("prefix", ["pl.ci-", "pl-cli-lease-postgres-", "-_"])
def test_start_takes_a_plain_prefix(fakes, prefix):
    """The near misses: one dot, the lease fixture's own prefix, an empty one."""
    assert postgres.start(fakes.parent, prefix).owned


def test_start_rotates_off_the_command_line_password(fakes):
    server = postgres.start(fakes.parent, "pl-test-")
    argv_password = fakes.pg0.instances[0]["password"]
    assert server.password != argv_password
    assert fakes.server.accepts(server.password) and not fakes.server.accepts(argv_password)
    # Only the verifier crosses the connection, never the password itself.
    assert not any(server.password in text for text in fakes.server.statements)


def test_start_refuses_when_the_old_password_still_works(fakes):
    fakes.server.keep_old = True
    with pytest.raises(postgres.SetupError, match="still accepts the start-up password"):
        postgres.start(fakes.parent, "pl-test-")
    assert len(fakes.pg0.drops) == 1  # the owned server is stopped, not left running
    assert list(fakes.parent.iterdir()) == []  # and its fresh directory is gone


def test_start_refuses_a_server_that_does_not_serve_its_directory(fakes):
    original = fakes.server.boot
    fakes.server.boot = lambda data_dir, password: original(data_dir.parent, password)
    with pytest.raises(postgres.SetupError, match="does not serve the fresh data directory"):
        postgres.start(fakes.parent, "pl-test-")
    # It answers for another directory, so it is not confirmed ours: nothing
    # is signalled and the fresh directory (with its pid file) stays.
    assert fakes.stops == [] and fakes.pg0.drops == []
    (data_dir,) = fakes.parent.iterdir()
    assert (data_dir / "pgdata" / "postmaster.pid").is_file()


def test_start_refuses_a_server_that_was_already_running(fakes):
    fakes.pg0.already_running = True
    with pytest.raises(postgres.SetupError, match="refusing to adopt it"):
        postgres.start(fakes.parent, "pl-test-")
    assert fakes.pg0.drops == []
    assert list(fakes.parent.iterdir()) == []


def test_start_refuses_a_server_outside_its_fresh_directory(fakes):
    fakes.pg0.pidfile = False
    with pytest.raises(postgres.SetupError, match="refusing to adopt it"):
        postgres.start(fakes.parent, "pl-test-")
    assert fakes.stops == []  # nothing of ours runs
    assert not list((fakes.home / ".pg0" / "instances").iterdir())  # and pg0's record of it goes
    assert list(fakes.parent.iterdir()) == []


def test_start_removes_its_directory_when_pg0_fails_before_launching(fakes):
    """An initdb failure (an unknown locale, say) leaves no server: the
    fresh directory goes and nothing is stopped."""
    fakes.pg0.pidfile, fakes.pg0.fail_after_launch = False, True
    with pytest.raises(_Pg0Error):
        postgres.start(fakes.parent, "pl-test-")
    assert fakes.stops == []
    assert list(fakes.parent.iterdir()) == []


@pytest.mark.parametrize("field", ["info_port", "info_pid"])
def test_start_stops_its_own_server_when_pg0_reports_another(fakes, field):
    setattr(fakes.pg0, field, 999)
    with pytest.raises(postgres.SetupError, match="another server"):
        postgres.start(fakes.parent, "pl-test-")
    assert len(fakes.pg0.drops) == 1


def test_start_keeps_the_directory_of_a_server_it_could_not_stop(fakes, monkeypatch):
    """A server still running in the fresh directory keeps its files."""
    monkeypatch.setattr(postgres, "_signal_shutdown", lambda pid: fakes.stops.append(pid))
    fakes.server.honour_alter = False
    with pytest.raises(postgres.SetupError):
        postgres.start(fakes.parent, "pl-test-")
    assert len(fakes.stops) == 1 and fakes.pg0.drops == []
    (data_dir,) = fakes.parent.iterdir()
    assert (data_dir / "pgdata" / "postmaster.pid").is_file()


def test_start_stops_a_server_pg0_launched_before_failing(fakes):
    fakes.pg0.fail_after_launch = True
    with pytest.raises(_Pg0Error):
        postgres.start(fakes.parent, "pl-test-")
    assert len(fakes.pg0.drops) == 1
    assert list(fakes.parent.iterdir()) == []


def test_start_uses_its_own_server_when_pg0_omits_the_uri(fakes):
    original = fakes.pg0.Pg0.start

    def start(self):
        info = original(self)
        info.uri, info.port, info.pid = None, None, None
        return info

    fakes.pg0.Pg0.start = start
    server = postgres.start(fakes.parent, "pl-test-")
    assert (server.port, server.pid, server.host) == (55432, 4242, "127.0.0.1")


@pytest.mark.parametrize("host", ["10.0.0.5", "127.0.0.2", "0.0.0.0", "::1", "example.com"])
def test_start_refuses_a_uri_host_other_than_the_verified_one(fakes, host):
    """Every check connects to 127.0.0.1, so only 127.0.0.1 is exported:
    pg0 naming any other host, loopback or not, is refused."""
    fakes.pg0.uri_host = f"[{host}]" if ":" in host else host
    with pytest.raises(postgres.SetupError, match="another host than 127.0.0.1.*requirements-postgres.txt"):
        postgres.start(fakes.parent, "pl-test-")
    assert len(fakes.stops) == 1  # our own server is stopped


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_start_exports_only_the_verified_host(fakes, host):
    fakes.pg0.uri_host = host
    server = postgres.start(fakes.parent, "pl-test-")
    assert server.host == "127.0.0.1"
    assert server.admin_conninfo().startswith("host=127.0.0.1 ")


def test_pg0_and_connections_never_see_the_runners_libpq_variables(fakes, monkeypatch):
    monkeypatch.setenv("PGHOSTADDR", "10.0.0.5")
    monkeypatch.setenv("PGSSLMODE", "require")
    postgres.start(fakes.parent, "pl-test-")
    assert not any(key.upper().startswith("PG") for key in fakes.pg0.env_during_start)
    assert os.environ["PGHOSTADDR"] == "10.0.0.5"  # restored afterwards


def test_connect_pins_the_loopback_address_and_hides_libpq_variables(monkeypatch):
    monkeypatch.setenv("PGHOSTADDR", "10.0.0.5")
    seen = {}

    def connect(**kwargs):
        seen.update(kwargs, environment=dict(os.environ))
        return "connection"

    monkeypatch.setattr(psycopg, "connect", connect)
    assert postgres._connect(55432, "synthetic-secret") == "connection"
    assert seen["hostaddr"] == "127.0.0.1" and seen["sslmode"] == "disable"
    assert "PGHOSTADDR" not in seen["environment"]
    assert os.environ["PGHOSTADDR"] == "10.0.0.5"


def test_login_tells_refused_from_unreachable(fakes):
    fakes.server.boot(fakes.parent, "right")
    assert postgres._login(1, "wrong") == "refused"
    assert postgres._login(1, "right") == "ok"
    fakes.server.running = False
    assert postgres._login(1, "wrong") == "unreachable"  # no answer proves nothing


def test_start_requires_a_login_with_the_rotated_password(fakes):
    fakes.server.down_after_alter = True
    with pytest.raises(postgres.SetupError, match="did not open with the rotated password"):
        postgres.start(fakes.parent, "pl-test-")


def test_start_refuses_when_the_old_password_cannot_be_checked(fakes, monkeypatch):
    real = postgres._login
    monkeypatch.setattr(postgres, "_login", lambda port, password: "unreachable"
                        if password == fakes.pg0.instances[0]["password"] else real(port, password))
    with pytest.raises(postgres.SetupError, match="could not be asked"):
        postgres.start(fakes.parent, "pl-test-")


def test_a_failed_start_leaves_a_server_that_never_answers_as_ours(fakes):
    """pg0 failed after writing a pid file, but nothing answers on its port:
    that pid may be anyone's, so nothing is signalled and nothing deleted."""
    fakes.pg0.answers, fakes.pg0.fail_after_launch = False, True
    with pytest.raises(_Pg0Error):
        postgres.start(fakes.parent, "pl-test-")
    assert fakes.stops == [] and fakes.pg0.drops == []
    (data_dir,) = fakes.parent.iterdir()
    assert (data_dir / "pgdata" / "postmaster.pid").is_file()


def test_stop_reports_failure_while_the_postmaster_survives(fakes, monkeypatch):
    server = postgres.start(fakes.parent, "pl-test-")
    monkeypatch.setattr(postgres, "_signal_shutdown", lambda pid: fakes.stops.append(pid))
    assert server.stop() is False
    assert len(fakes.stops) == 1 and fakes.pg0.drops == []  # registry kept, files kept
    assert server.data_dir.exists() and server.owned


def test_shutdown_signals_the_verified_pid_itself(monkeypatch):
    """POSIX: SIGINT (fast shutdown) to that pid. Windows: the byte on the
    postmaster's own signal pipe, as pg_ctl's pgkill sends it."""
    import signal as signal_module
    sent = []
    monkeypatch.setattr(postgres.os, "name", "posix")
    monkeypatch.setattr(postgres.os, "kill", lambda pid, sig: sent.append(("kill", pid, sig)))
    postgres._signal_shutdown(4242)
    assert sent == [("kill", 4242, signal_module.SIGINT)]

    monkeypatch.setattr(postgres.os, "name", "nt")
    api = FakeWinApi(monkeypatch)
    postgres._signal_shutdown(4242)
    name = r"\\.\pipe\pgsignal_4242"
    assert api.calls == [("CreateFile", name, api.GENERIC_READ | api.GENERIC_WRITE, api.OPEN_EXISTING),
                         ("WriteFile", "handle", b"\x02"), ("ReadFile", "handle", 1),
                         ("CloseHandle", "handle")]


def test_exited_waits_for_the_pid_file_to_go(tmp_path):
    (tmp_path / "postmaster.pid").write_text("4242\nx\n0\n55432\n", encoding="utf-8")
    assert postgres._exited(tmp_path, 4242, 0) is False
    (tmp_path / "postmaster.pid").unlink()
    assert postgres._exited(tmp_path, 4242, 0) is True


def test_exited_answers_at_once_when_another_server_holds_the_directory(tmp_path, monkeypatch):
    """Not an exit, and no point waiting for one: the answer is immediate."""
    (tmp_path / "postmaster.pid").write_text("5151\nx\n0\n55432\n", encoding="utf-8")

    def sleep(seconds):
        raise AssertionError("waited for a pid file another server holds")

    monkeypatch.setattr(postgres.time, "sleep", sleep)
    assert postgres._exited(tmp_path, 4242, 60) is False


def test_stop_reports_failure_when_the_signal_cannot_be_sent(fakes, monkeypatch):
    server = postgres.start(fakes.parent, "pl-test-")

    def refuse(pid):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(postgres, "_signal_shutdown", refuse)
    assert server.stop() is False and fakes.pg0.drops == [] and server.data_dir.exists()

def test_scram_verifier_has_postgres_layout():
    verifier = postgres.scram_verifier("synthetic", salt=b"\x00" * 16)
    assert re.fullmatch(r"SCRAM-SHA-256\$4096:[A-Za-z0-9+/=]{24}\$[A-Za-z0-9+/=]{44}:[A-Za-z0-9+/=]{44}", verifier)
    assert verifier == postgres.scram_verifier("synthetic", salt=b"\x00" * 16)
    assert verifier != postgres.scram_verifier("synthetic", salt=b"\x01" * 16)


def test_stop_stops_only_the_server_it_started(fakes):
    server = postgres.start(fakes.parent, "pl-test-")
    assert server.stop() is True
    assert fakes.stops == [server.pid] and fakes.pg0.drops == [server.name]
    assert not server.data_dir.exists()
    assert server.stop() is False and fakes.pg0.drops == [server.name]


def test_stop_leaves_an_attached_server_running(fakes, tmp_path):
    pgdata = tmp_path / "pgdata"
    pgdata.mkdir()
    (pgdata / "postmaster.pid").write_text("4242\nx\n0\n55432\n", encoding="utf-8")
    fakes.server.boot(pgdata, "x")
    server = postgres.Server(name="theirs", data_dir=tmp_path, pgdata=pgdata, port=55432, pid=4242,
                             password="x", owned=False, started=START_TIME)
    assert server.stop() is False
    assert fakes.pg0.drops == []


def test_stop_leaves_a_server_that_took_the_pid_file_after_answering(fakes, monkeypatch):
    """The pid file pg_ctl signals must still name the server that just
    answered: a restart between the check and the stop is left alone."""
    server = postgres.start(fakes.parent, "pl-test-")
    real = postgres._identity

    def answer_then_restart(port, password):
        identity = real(port, password)
        (server.pgdata / "postmaster.pid").write_text("5151\nx\n0\n55432\n", encoding="utf-8")
        return identity

    monkeypatch.setattr(postgres, "_identity", answer_then_restart)
    assert server.stop() is False
    assert fakes.stops == [] and fakes.pg0.drops == []


def test_stop_leaves_a_server_that_replaced_its_own(fakes):
    server = postgres.start(fakes.parent, "pl-test-")
    (server.pgdata / "postmaster.pid").write_text("5151\nx\n0\n55432\n", encoding="utf-8")
    assert server.stop() is False
    assert fakes.pg0.drops == []
    assert server.data_dir.exists()


@pytest.mark.parametrize("change", ["restarted", "unreachable", "other_directory"])
def test_stop_leaves_a_server_it_cannot_identify(fakes, change):
    """A stale postmaster.pid whose pid another process reuses must not be
    signalled: the server must still answer as the one this process started."""
    server = postgres.start(fakes.parent, "pl-test-")
    if change == "restarted":
        fakes.server.started = "2026-11-24 05:00:00+00"
    elif change == "unreachable":
        fakes.server.running = False
    else:
        fakes.server.data_directory = str(fakes.parent)
    assert server.stop() is False
    assert fakes.pg0.drops == []


def test_no_exception_leaves_start_with_the_start_up_password(fakes):
    """pg0's stderr can carry the password it was given on its argv."""
    fakes.pg0.fail_after_launch = True
    original = fakes.pg0.Pg0.start

    def start(self):
        try:
            original(self)
        except _Pg0Error as error:
            raise _Pg0Error(f"pg0 start --password {self.kwargs['password']} failed") from error

    fakes.pg0.Pg0.start = start
    with pytest.raises(_Pg0Error) as raised:
        postgres.start(fakes.parent, "pl-test-")
    password = fakes.pg0.instances[0]["password"]
    assert password not in str(raised.value) and "***" in str(raised.value)
    assert all(password not in str(arg) for error in _chain(raised.value) for arg in error.args)


def _chain(error):
    """Every exception reachable through __cause__ and __context__."""
    seen, pending = [], [error]
    while pending:
        error = pending.pop()
        if error is not None and all(error is not other for other in seen):
            seen.append(error)
            pending += [error.__cause__, error.__context__]
    return seen


def test_scrub_walks_both_the_cause_and_the_context():
    try:
        try:
            raise OSError("context with secret-b")
        except OSError:
            cause = ValueError("cause with secret-a")
            cause.__context__ = KeyError("deeper with secret-a and secret-b")
            raise RuntimeError("top with secret-a") from cause
    except RuntimeError as error:
        top = error
    top.__context__.__context__ = top  # a cycle ends the walk, not the process
    postgres._scrub(top, ["secret-a", "secret-b"])
    chain = _chain(top)
    assert len(chain) == 4
    assert all("secret" not in str(arg) for error in chain for arg in error.args)
    assert all("***" in str(error.args[0]) for error in chain)


def test_no_exception_leaves_start_with_the_rotated_password(fakes, monkeypatch):
    """A failure after rotation carries the new password: it is scrubbed
    throughout the chain, on the context branch beside a cause too."""
    seen = []

    def login(port, password):
        seen.append(password)
        try:
            raise OSError(f"socket note for {password}")
        except OSError:
            raise RuntimeError("login probe failed") from ValueError(f"probe of {password}")

    monkeypatch.setattr(postgres, "_login", login)
    with pytest.raises(RuntimeError) as raised:
        postgres.start(fakes.parent, "pl-test-")
    rotated, first = seen[0], fakes.pg0.instances[0]["password"]
    assert rotated != first
    chain = _chain(raised.value)
    assert len(chain) == 3
    text = " ".join(str(arg) for error in chain for arg in error.args)
    assert rotated not in text and first not in text and text.count("***") == 2


def test_a_partly_written_pid_file_is_not_an_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(postgres, "PIDFILE_SETTLE", 0)
    (tmp_path / "postmaster.pid").write_text("42", encoding="utf-8")  # the postmaster mid-write
    assert postgres._exited(tmp_path, 4242, 0) is False
    (tmp_path / "postmaster.pid").unlink()
    assert postgres._exited(tmp_path, 4242, 0) is True


def test_a_failed_start_leaves_a_directory_whose_pid_file_cannot_be_read(fakes):
    fakes.pg0.pidfile, fakes.pg0.fail_after_launch = False, True
    original = fakes.pg0.Pg0.start

    def start(self):
        (Path(self.kwargs["data_dir"]) / "postmaster.pid").write_text("42", encoding="utf-8")
        original(self)

    fakes.pg0.Pg0.start = start
    with pytest.raises(_Pg0Error):
        postgres.start(fakes.parent, "pl-test-")
    assert fakes.stops == []
    (data_dir,) = fakes.parent.iterdir()  # something may run there: kept
    assert (data_dir / "pgdata" / "postmaster.pid").is_file()


def test_start_names_an_unreadable_pid_file_after_a_good_start(fakes):
    original = fakes.pg0.Pg0.start

    def start(self):
        info = original(self)
        (Path(self.kwargs["data_dir"]) / "postmaster.pid").write_text("42", encoding="utf-8")
        return info

    fakes.pg0.Pg0.start = start
    with pytest.raises(postgres.SetupError, match="pid file in .* cannot be read; stop any server") as raised:
        postgres.start(fakes.parent, "pl-test-")
    assert "already running" not in str(raised.value)
    assert fakes.stops == [] and len(list(fakes.parent.iterdir())) == 1  # something may run there: kept


def test_pid_file_settle_waits_for_a_complete_file(tmp_path, monkeypatch):
    monkeypatch.setattr(postgres, "PIDFILE_SETTLE", 5)
    (tmp_path / "postmaster.pid").write_text("42", encoding="utf-8")
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        (tmp_path / "postmaster.pid").write_text("4242\nx\n0\n55432\n", encoding="utf-8")

    monkeypatch.setattr(postgres.time, "sleep", sleep)
    assert postgres._pidfile_settled(tmp_path) == ((4242, 55432), True)
    assert sleeps


def test_deregister_removes_only_the_record_of_its_own_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    ours, theirs = tmp_path / "ours" / "pgdata", tmp_path / "theirs" / "pgdata"
    for name, data_dir in (("mine", ours), ("other", theirs)):
        record = tmp_path / ".pg0" / "instances" / name
        record.mkdir(parents=True)
        (record / "instance.json").write_text(json.dumps({"data_dir": str(data_dir)}), encoding="utf-8")
    postgres._deregister("mine", ours)
    postgres._deregister("other", ours)  # a record naming another directory stays
    assert not (tmp_path / ".pg0" / "instances" / "mine").exists()
    assert (tmp_path / ".pg0" / "instances" / "other").exists()


@pytest.mark.parametrize("document", ["[]", '{"data_dir": null}', '{"data_dir": 7}', '{"data_dir": ""}',
                                      '"text"', "null"])
def test_deregister_leaves_a_record_of_another_shape(tmp_path, monkeypatch, document):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    record = tmp_path / ".pg0" / "instances" / "mine"
    record.mkdir(parents=True)
    (record / "instance.json").write_text(document, encoding="utf-8")
    postgres._deregister("mine", tmp_path / "ours" / "pgdata")
    assert (record / "instance.json").read_text(encoding="utf-8") == document


def test_stop_removes_the_directory_beside_a_record_of_another_shape(fakes):
    server = postgres.start(fakes.parent, "pl-test-")
    (fakes.home / ".pg0" / "instances" / server.name / "instance.json").write_text("[]", encoding="utf-8")
    assert server.stop() is True and not server.data_dir.exists()


def test_a_record_of_another_shape_keeps_the_start_error(fakes):
    fakes.pg0.fail_after_launch = True
    original = fakes.pg0.Pg0.start

    def start(self):
        try:
            original(self)
        finally:
            record = Path.home() / ".pg0" / "instances" / self.kwargs["name"] / "instance.json"
            record.write_text('{"data_dir": null}', encoding="utf-8")

    fakes.pg0.Pg0.start = start
    with pytest.raises(_Pg0Error, match="timed out"):
        postgres.start(fakes.parent, "pl-test-")
    assert len(fakes.stops) == 1 and list(fakes.parent.iterdir()) == []


def test_a_busy_signal_pipe_waits_for_a_free_instance(monkeypatch):
    """CreateFile reports a busy pipe as ERROR_PIPE_BUSY; WaitNamedPipe then
    waits for the next free instance and the byte goes through."""
    monkeypatch.setattr(postgres.os, "name", "nt")
    api = FakeWinApi(monkeypatch, busy=2, wait_takes=0.5)
    postgres._signal_shutdown(4242)
    assert api.names() == ["CreateFile", "WaitNamedPipe", "CreateFile", "WaitNamedPipe", "CreateFile",
                           "WriteFile", "ReadFile", "CloseHandle"]
    waits = [call[2] for call in api.calls if call[0] == "WaitNamedPipe"]
    assert waits == [5000, 4500]  # what is left of the 5 s, in milliseconds


def test_a_signal_pipe_busy_for_five_seconds_gives_up(monkeypatch):
    monkeypatch.setattr(postgres.os, "name", "nt")
    api = FakeWinApi(monkeypatch, busy=10**6, wait_takes=2.0, wait_error=FakeWinApi.ERROR_SEM_TIMEOUT)
    started = api.now
    with pytest.raises(OSError) as raised:
        postgres._signal_shutdown(4242)
    assert raised.value.winerror == api.ERROR_PIPE_BUSY
    assert api.now - started == pytest.approx(postgres.PIPE_GIVE_UP + 1.0)  # three waits of 2 s
    assert api.names().count("WaitNamedPipe") == 3 and "WriteFile" not in api.names()
    assert "CloseHandle" not in api.names()  # nothing was opened


def test_a_busy_signal_pipe_is_a_failed_stop_not_an_error(fakes, monkeypatch):
    server = postgres.start(fakes.parent, "pl-test-")
    monkeypatch.setattr(postgres, "_signal_shutdown", postgres.__dict__["_signal_pipe"])
    FakeWinApi(monkeypatch, busy=10**6, wait_takes=3.0, wait_error=FakeWinApi.ERROR_SEM_TIMEOUT)
    assert server.stop() is False and server.data_dir.exists() and server.owned


@pytest.mark.parametrize("winerror", [2, 5, 22])  # gone, denied, invalid
def test_another_pipe_error_is_not_retried(monkeypatch, winerror):
    monkeypatch.setattr(postgres.os, "name", "nt")
    api = FakeWinApi(monkeypatch, create_error=FakeWinApi._error(winerror, "refused"))
    with pytest.raises(OSError) as raised:
        postgres._signal_shutdown(4242)
    assert raised.value.winerror == winerror and api.names() == ["CreateFile"]


def test_a_failed_wait_is_not_retried(monkeypatch):
    monkeypatch.setattr(postgres.os, "name", "nt")
    api = FakeWinApi(monkeypatch, busy=1, wait_error=2)  # the pipe went away
    with pytest.raises(OSError) as raised:
        postgres._signal_shutdown(4242)
    assert raised.value.winerror == 2 and api.names() == ["CreateFile", "WaitNamedPipe"]


def test_the_signal_pipe_is_closed_when_the_acknowledgement_fails(monkeypatch):
    monkeypatch.setattr(postgres.os, "name", "nt")
    api = FakeWinApi(monkeypatch, read_error=FakeWinApi._error(109, "The pipe has been ended"))
    postgres._signal_shutdown(4242)
    assert api.names() == ["CreateFile", "WriteFile", "ReadFile", "CloseHandle"]


def test_the_native_child_opens_no_console_window_on_windows(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(postgres.subprocess, "run", lambda command, **kwargs: seen.update(kwargs) or
                        subprocess.CompletedProcess(command, 0, "", ""))
    server = postgres.Server(name="n", data_dir=tmp_path, pgdata=tmp_path, port=55432, pid=1,
                             password="synthetic-secret", owned=True)
    binary, login, real = Path("bin/pseudolife-stdio"), tmp_path / "test-pg.env", os.name
    flags = {}
    # pathlib cannot make paths while os.name is patched: every path exists
    # before, and os.name is restored before anything is asserted.
    for name in ("nt", "posix"):
        monkeypatch.setattr(postgres.os, "name", name)
        try:
            postgres.create_test_login(binary, server, login)
        finally:
            monkeypatch.setattr(postgres.os, "name", real)
        flags[name] = seen["creationflags"]
    assert flags == {"nt": 0x08000000, "posix": 0}


def test_child_environment_drops_libpq_controls_and_the_daemon_url():
    server = postgres.Server(name="n", data_dir=Path("d"), pgdata=Path("d/p"), port=1, pid=1,
                             password="synthetic-secret", owned=False)
    env = postgres.child_environment(server, {"PATH": "p", "PGHOST": "h", "pgport": "1",
                                              "PGPASSFILE": "f", "PSEUDOLIFE_MCP_DATABASE_URL": "u"})
    assert env == {"PATH": "p", "PGPASSWORD": "synthetic-secret"}


def test_create_test_login_runs_the_native_client_without_the_password_on_argv(monkeypatch, tmp_path):
    calls = []

    def run(command, **kwargs):
        profile = Path(kwargs["env"]["HOME"])
        calls.append((command, kwargs, profile.is_dir() and not any(profile.iterdir())))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(postgres.subprocess, "run", run)
    server = postgres.Server(name="n", data_dir=tmp_path, pgdata=tmp_path, port=55432, pid=1,
                             password="synthetic-secret", owned=True)
    login = tmp_path / "test-pg.env"
    postgres.create_test_login(Path("bin/pseudolife-stdio"), server, login)
    (command, kwargs, empty_profile), = calls
    # An empty profile of its own: the runner's ~/.postgresql (or APPDATA's
    # postgresql) client files cannot refuse the admin URL.
    env = kwargs["env"]
    assert empty_profile and env["HOME"] == env["USERPROFILE"] == env["APPDATA"]
    assert Path(env["HOME"]) != Path.home() and not Path(env["HOME"]).exists()
    assert command == [str(Path("bin/pseudolife-stdio")), "test-login", "create", "--rotate", "--json",
                       "--file", str(login), "--admin-url",
                       "host=127.0.0.1 port=55432 user=postgres dbname=postgres"]
    assert not any("synthetic-secret" in part for part in command)
    assert kwargs["env"]["PGPASSWORD"] == "synthetic-secret"
    assert kwargs["timeout"] < 300  # inside the shortest calling step's five minutes


@pytest.mark.parametrize(("stdout", "stderr", "reason"), [
    ('{"exit": 3, "error": "the admin login is not a superuser", "changes": []}\n', "", "the admin login is not a superuser"),
    ('{"exit": 2, "error": null, "changes": []}\n', "usage: --file is required", "usage: --file is required"),
    ("", "", "no reason printed (exit 3)"),
])
def test_failure_reason_reads_the_json_report_first(stdout, stderr, reason):
    assert postgres.failure_reason(subprocess.CompletedProcess([], 3, stdout, stderr)) == reason


def test_the_pins_match_the_package_extras_they_replace():
    """rust/ci/requirements-postgres.txt carries the pins pyproject's
    dependencies bring today, so the package's deletion cannot loosen them."""
    pins = [line.strip() for line in (ROOT / "rust/ci/requirements-postgres.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = set(project["dependencies"]) | {pin for extra in project["optional-dependencies"].values() for pin in extra}
    assert pins == ["pg0-embedded>=0.15.2,<0.16", "psycopg[binary]>=3.3.8"]
    assert set(pins) <= declared


# ── the lease row's fixture ──────────────────────────────────────────────────

class _BlockPackage:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "pseudolife_memory" or fullname.startswith("pseudolife_memory."):
            raise AssertionError(f"unexpected product package import: {fullname}")


@contextlib.contextmanager
def package_blocked():
    saved = {name: module for name, module in sys.modules.items()
             if name == "pseudolife_memory" or name.startswith("pseudolife_memory.")}
    for name in saved:
        del sys.modules[name]
    try:
        with patch.object(sys, "meta_path", [_BlockPackage(), *sys.meta_path]):
            yield
    finally:
        sys.modules.update(saved)


def test_neither_module_imports_the_package_when_loaded():
    names = ("rust.ci.disposable_postgres", "rust.cli_harness.lease_ci")
    parents = [(importlib.import_module(name.rpartition(".")[0]), name.rpartition(".")[2]) for name in names]
    bound = [vars(parent).get(child) for parent, child in parents]
    try:
        with package_blocked(), patch.dict(sys.modules):
            for name in names:
                sys.modules.pop(name, None)
                importlib.import_module(name)
    finally:
        # The re-import rebinds each parent package's attribute; later tests
        # patch the module objects the rest of this file imported.
        for (parent, child), module in zip(parents, bound):
            if module is None:
                vars(parent).pop(child, None)
            else:
                setattr(parent, child, module)


@pytest.fixture
def lease_ci(monkeypatch, tmp_path, fakes):
    binary = tmp_path / "pseudolife-stdio"
    binary.write_bytes(b"")
    env_file = tmp_path / "github-env"
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "runner-temp"))
    monkeypatch.setenv("GITHUB_ENV", str(env_file))
    monkeypatch.setenv("PSEUDOLIFE_LEASE_CI_BINARY", str(binary))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        login = Path(command[command.index("--file") + 1])
        login.parent.mkdir(parents=True, exist_ok=True)
        login.write_text("PSEUDOLIFE_TEST_PG_USER=pseudolife_test\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, json.dumps({"exit": 0, "error": None}), "")

    monkeypatch.setattr(postgres.subprocess, "run", run)
    with package_blocked():
        module = importlib.import_module("rust.cli_harness.lease_ci")
        monkeypatch.setattr(module, "_DATABASE_TURN_MARKER", tmp_path / "no-turn-marker")
        yield types.SimpleNamespace(module=module, calls=calls, env_file=env_file, binary=binary,
                                    tmp=tmp_path, fakes=fakes)


def test_lease_fixture_exports_the_cluster_and_login(lease_ci, capsys):
    assert lease_ci.module.main() == 0
    assert lease_ci.fakes.stops == []  # the next steps use it
    lines = lease_ci.env_file.read_text(encoding="utf-8").splitlines()
    login = lease_ci.tmp / "runner-temp" / "test-pg.env"
    assert lines == ["PSEUDOLIFE_TEST_PG_HOST_PORT=127.0.0.1:55432", f"PSEUDOLIFE_TEST_PG_LOGIN_FILE={login}"]
    (command, kwargs), = lease_ci.calls
    assert command[:3] == [str(lease_ci.binary), "test-login", "create"]
    assert "PSEUDOLIFE_MCP_DATABASE_URL" not in kwargs["env"]
    # The cluster lives under the home directory, not RUNNER_TEMP (initdb's chmod).
    assert Path(lease_ci.fakes.pg0.instances[0]["data_dir"]).parent.parent == lease_ci.tmp


def test_lease_fixture_runs_the_binary_it_checked(lease_ci, monkeypatch):
    monkeypatch.chdir(lease_ci.tmp)
    monkeypatch.setenv("PSEUDOLIFE_LEASE_CI_BINARY", "pseudolife-stdio")
    assert lease_ci.module.main() == 0
    (command, _), = lease_ci.calls
    assert Path(command[0]).is_absolute() and Path(command[0]) == lease_ci.binary


def test_lease_fixture_refuses_a_login_it_cannot_find(lease_ci, monkeypatch, capsys):
    monkeypatch.setattr(postgres.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0, json.dumps({"exit": 0, "error": None}), ""))
    assert lease_ci.module.main() == 1
    err = capsys.readouterr().err
    assert "wrote no login file" in err and "PSEUDOLIFE_LEASE_CI_BINARY" in err
    assert not lease_ci.env_file.exists() and len(lease_ci.fakes.stops) == 1


def test_lease_fixture_refuses_without_the_native_client(lease_ci, capsys):
    lease_ci.binary.unlink()
    assert lease_ci.module.main() == 1
    assert "native client is missing" in capsys.readouterr().err
    assert lease_ci.fakes.pg0.instances == [] and lease_ci.calls == []


def test_lease_fixture_reports_why_the_login_failed_without_its_secret(lease_ci, monkeypatch, capsys):
    def run(command, **kwargs):
        secret = kwargs["env"]["PGPASSWORD"]
        seen.append(secret)
        # The report's reason, carrying the bare secret: only the redaction by
        # value can remove it.
        report = {"exit": 3, "error": f"admin connection refused for {secret}", "changes": []}
        return subprocess.CompletedProcess(command, 3, json.dumps(report) + "\n", "")

    seen = []
    monkeypatch.setattr(postgres.subprocess, "run", run)
    assert lease_ci.module.main() == 3
    err = capsys.readouterr().err
    assert "test-login fixture provisioning failed (exit 3)" in err
    assert "admin connection refused for ***" in err
    assert not lease_ci.env_file.exists()
    assert seen and seen[0] not in err
    # Nobody can use a cluster with no login: it is stopped and removed.
    assert len(lease_ci.fakes.stops) == 1
    assert "still running" not in err
    assert not list(lease_ci.tmp.glob("pl-cli-lease-postgres-*"))


def test_lease_fixture_reports_a_failed_cleanup_beside_the_setup_error(lease_ci, monkeypatch, capsys):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 3, '{"exit": 3, "error": "refused"}', "")

    def boom(pid):
        raise OSError("signal pipe unavailable")

    monkeypatch.setattr(postgres.subprocess, "run", run)
    monkeypatch.setattr(postgres, "_signal_shutdown", boom)
    assert lease_ci.module.main() == 3  # the setup's own exit, not the cleanup's
    err = capsys.readouterr().err
    assert "test-login fixture provisioning failed (exit 3)" in err
    assert "is still running" in err and "stop it by that directory" in err


def test_lease_fixture_keeps_the_setup_exit_when_cleanup_raises(lease_ci, monkeypatch, capsys):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 3, '{"exit": 3, "error": "refused"}', "")

    def stop(self):
        raise RuntimeError("cleanup broke")

    monkeypatch.setattr(postgres.subprocess, "run", run)
    monkeypatch.setattr(postgres.Server, "stop", stop)
    assert lease_ci.module.main() == 3
    err = capsys.readouterr().err
    assert "cleanup failed (RuntimeError)" in err and "is still running" in err


def test_lease_fixture_stops_the_cluster_when_the_native_client_cannot_run(lease_ci, monkeypatch, capsys):
    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 240)

    monkeypatch.setattr(postgres.subprocess, "run", run)
    assert lease_ci.module.main() == 1
    assert "TimeoutExpired" in capsys.readouterr().err
    assert len(lease_ci.fakes.stops) == 1 and not lease_ci.env_file.exists()


def test_lease_fixture_refuses_values_github_env_cannot_hold_before_starting(lease_ci, monkeypatch, capsys):
    monkeypatch.setenv("RUNNER_TEMP", str(lease_ci.tmp / "runner\ntemp"))
    assert lease_ci.module.main() == 1
    assert "cannot be written to GITHUB_ENV" in capsys.readouterr().err
    assert not lease_ci.env_file.exists()
    assert lease_ci.fakes.pg0.instances == []


def test_lease_fixture_imports_nothing_from_the_package(lease_ci):
    assert lease_ci.module.main() == 0
    assert not any(name.startswith("pseudolife_memory") for name in sys.modules)


# ── the command line the Windows ci.yml lanes run ────────────────────────────

@pytest.fixture
def command_line(fakes, monkeypatch, tmp_path):
    env_file = tmp_path / "github-env"
    monkeypatch.setenv("GITHUB_ENV", str(env_file))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return types.SimpleNamespace(env_file=env_file, fakes=fakes, tmp=tmp_path)


def test_command_line_exports_a_superuser_url_and_leaves_the_cluster_running(command_line, capsys):
    assert postgres.main(["--github-env", "PSEUDOLIFE_TEST_DATABASE_URL", "--database", "pseudolife_memory_test"]) == 0
    line, = command_line.env_file.read_text(encoding="utf-8").splitlines()
    name, _, url = line.partition("=")
    assert name == "PSEUDOLIFE_TEST_DATABASE_URL"
    assert re.fullmatch(r"postgresql://postgres:[0-9a-f]{32}@127\.0\.0\.1:55432/pseudolife_memory_test", url)
    password = url.split(":")[2].split("@")[0]
    assert command_line.fakes.server.accepts(password)  # the rotated password, not pg0's start-up one
    out = capsys.readouterr().out
    assert f"::add-mask::{password}" in out and out.index("::add-mask::") < out.index("ready")
    assert command_line.fakes.stops == []  # later steps use it
    started = Path(command_line.fakes.pg0.instances[0]["data_dir"])
    assert started.parent.parent == command_line.tmp  # under home, not RUNNER_TEMP


@pytest.mark.parametrize("argv", [["--github-env", "lower", "--database", "ok_db"],
                                  ["--github-env", "A=B", "--database", "ok_db"],
                                  ["--github-env", "OK", "--database", "Bad-Name"],
                                  ["--github-env", "OK"],
                                  ["--github-env", "OK", "--database", "postgres"],
                                  ["--github-env", "OK", "--database", "template1"], []])
def test_command_line_refuses_bad_arguments_before_starting(command_line, argv):
    with pytest.raises(SystemExit) as refused:
        postgres.main(argv)
    assert refused.value.code == 2
    assert command_line.fakes.pg0.instances == [] and not command_line.env_file.exists()


def test_command_line_refuses_outside_github_actions(command_line, monkeypatch):
    monkeypatch.delenv("GITHUB_ENV")
    with pytest.raises(SystemExit):
        postgres.main(["--github-env", "PSEUDOLIFE_TEST_DATABASE_URL", "--database", "pseudolife_memory_test"])
    assert command_line.fakes.pg0.instances == []


def test_command_line_stops_the_cluster_when_it_cannot_export(command_line, monkeypatch):
    monkeypatch.setenv("GITHUB_ENV", str(command_line.tmp / "missing-dir" / "github-env"))
    with pytest.raises(OSError):
        postgres.main(["--github-env", "PSEUDOLIFE_TEST_DATABASE_URL", "--database", "pseudolife_memory_test"])
    assert len(command_line.fakes.stops) == 1


def test_command_line_keeps_the_export_error_when_cleanup_raises(command_line, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ENV", str(command_line.tmp / "missing-dir" / "github-env"))

    def stop(self):
        raise RuntimeError("cleanup broke")

    monkeypatch.setattr(postgres.Server, "stop", stop)
    with pytest.raises(OSError):  # the export's error, not the cleanup's
        postgres.main(["--github-env", "PSEUDOLIFE_TEST_DATABASE_URL", "--database", "pseudolife_memory_test"])
    err = capsys.readouterr().err
    assert "cleanup failed (RuntimeError)" in err and "is still running" in err


def test_command_line_masks_only_under_github_actions(command_line, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ACTIONS")
    assert postgres.main(["--github-env", "PSEUDOLIFE_TEST_DATABASE_URL", "--database", "pseudolife_memory_test"]) == 0
    assert "::add-mask::" not in capsys.readouterr().out
