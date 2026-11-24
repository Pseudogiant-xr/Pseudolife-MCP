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
        self.instances, self.drops = [], []
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
                # pg0 drop: stop the server, delete its data directory.
                fake.drops.append(self.kwargs["name"])
                fake.server.running = False
                shutil.rmtree(self.kwargs["data_dir"], ignore_errors=True)

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


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    server = FakeServer()
    pg0 = FakePg0(server)
    pg0.Pg0Error = _Pg0Error
    monkeypatch.setitem(sys.modules, "pg0", pg0)
    monkeypatch.setattr(postgres, "_connect", server.connect)
    monkeypatch.setattr(postgres, "ABANDON_WAIT", 0)
    stops = []

    def pg_ctl_stop(pgdata):
        stops.append(pgdata)
        (pgdata / "postmaster.pid").unlink(missing_ok=True)
        server.running = False

    monkeypatch.setattr(postgres, "_pg_ctl_stop", pg_ctl_stop)
    return types.SimpleNamespace(pg0=pg0, server=server, parent=tmp_path, stops=stops)


def test_start_owns_a_fresh_loopback_cluster(fakes):
    server = postgres.start(fakes.parent, "pl-test-")
    assert server.owned and server.port == 55432 and server.pid == 4242 and server.host == "127.0.0.1"
    started = fakes.pg0.instances[0]
    assert started["config"] == {"listen_addresses": "127.0.0.1", "unix_socket_directories": "",
                                 "lc_messages": "C"}
    assert Path(started["data_dir"]).parent.parent == fakes.parent
    assert server.admin_conninfo() == "host=127.0.0.1 port=55432 user=postgres dbname=postgres"
    assert server.password not in server.admin_conninfo() and server.password not in repr(server)


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
    assert fakes.pg0.drops == []  # nothing of ours runs
    assert list(fakes.parent.iterdir()) == []


def test_start_removes_its_directory_when_pg0_fails_before_launching(fakes):
    """An initdb failure (an unknown locale, say) leaves no server: the
    fresh directory goes and nothing is stopped."""
    fakes.pg0.pidfile, fakes.pg0.fail_after_launch = False, True
    with pytest.raises(_Pg0Error):
        postgres.start(fakes.parent, "pl-test-")
    assert fakes.pg0.drops == []
    assert list(fakes.parent.iterdir()) == []


@pytest.mark.parametrize("field", ["info_port", "info_pid"])
def test_start_stops_its_own_server_when_pg0_reports_another(fakes, field):
    setattr(fakes.pg0, field, 999)
    with pytest.raises(postgres.SetupError, match="another server"):
        postgres.start(fakes.parent, "pl-test-")
    assert len(fakes.pg0.drops) == 1


def test_start_keeps_the_directory_of_a_server_it_could_not_stop(fakes, monkeypatch):
    """A server still running in the fresh directory keeps its files."""
    monkeypatch.setattr(postgres, "_pg_ctl_stop", lambda pgdata: fakes.stops.append(pgdata))
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


def test_start_refuses_a_non_loopback_endpoint(fakes):
    fakes.pg0.uri_host = "10.0.0.5"
    with pytest.raises(postgres.SetupError, match="loopback"):
        postgres.start(fakes.parent, "pl-test-")
    assert len(fakes.pg0.drops) == 1


@pytest.mark.parametrize("host", ["10.0.0.5", "0.0.0.0", "::1", "example.com", ""])
def test_loopback_host_refuses_everything_but_ipv4_loopback(host):
    with pytest.raises(postgres.SetupError):
        postgres.loopback_host(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "127.0.0.2"])
def test_loopback_host_accepts_ipv4_loopback(host):
    assert postgres.loopback_host(host).startswith("127.")


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
    monkeypatch.setattr(postgres, "_pg_ctl_stop", lambda pgdata: fakes.stops.append(pgdata))
    assert server.stop() is False
    assert len(fakes.stops) == 1 and fakes.pg0.drops == []  # registry kept, files kept
    assert server.data_dir.exists() and server.owned


def test_pg_ctl_comes_from_the_clusters_major_bundle(monkeypatch, tmp_path):
    home = tmp_path / "home"
    for version in ("17.6.0", "18.0.0", "18.1.0"):
        (home / ".pg0" / "installation" / version / "bin").mkdir(parents=True)
    pgdata = tmp_path / "pgdata"
    pgdata.mkdir()
    (pgdata / "PG_VERSION").write_text("18\n", encoding="ascii")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("PGHOST", "10.0.0.5")
    program, env = postgres._pg_ctl(pgdata)
    assert program.parent == home / ".pg0" / "installation" / "18.1.0" / "bin"
    assert program.name in ("pg_ctl", "pg_ctl.exe")
    assert "PGHOST" not in env
    if os.name != "nt":
        assert env["LD_LIBRARY_PATH"].split(os.pathsep)[0] == str(home / ".pg0" / "installation" / "18.1.0" / "lib")
    (pgdata / "PG_VERSION").write_text("19\n", encoding="ascii")
    with pytest.raises(postgres.SetupError, match="no pg0 PostgreSQL 19 bundle"):
        postgres._pg_ctl(pgdata)


def test_scram_verifier_has_postgres_layout():
    verifier = postgres.scram_verifier("synthetic", salt=b"\x00" * 16)
    assert re.fullmatch(r"SCRAM-SHA-256\$4096:[A-Za-z0-9+/=]{24}\$[A-Za-z0-9+/=]{44}:[A-Za-z0-9+/=]{44}", verifier)
    assert verifier == postgres.scram_verifier("synthetic", salt=b"\x00" * 16)
    assert verifier != postgres.scram_verifier("synthetic", salt=b"\x01" * 16)


def test_stop_stops_only_the_server_it_started(fakes):
    server = postgres.start(fakes.parent, "pl-test-")
    assert server.stop() is True
    assert fakes.stops == [server.pgdata] and fakes.pg0.drops == [server.name]
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
    assert not list(lease_ci.tmp.glob("pl-cli-lease-postgres-*"))


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
