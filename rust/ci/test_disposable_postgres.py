"""The CI PostgreSQL helper and the lease row's fixture, with pg0, the
server and the native client faked: nothing here starts a process."""
from __future__ import annotations

import contextlib
import importlib
import json
from pathlib import Path
import re
import subprocess
import sys
import types
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rust.ci import disposable_postgres as postgres  # noqa: E402


class _AlreadyRunning(Exception):
    pass


class FakePg0:
    """The pg0 module: ``start`` writes postmaster.pid as a server would."""

    def __init__(self, *, pid=4242, port=55432, uri_host="localhost", already_running=False,
                 pidfile=True, info_pid=None):
        self.pid, self.port, self.uri_host = pid, port, uri_host
        self.already_running, self.pidfile = already_running, pidfile
        self.info_pid = pid if info_pid is None else info_pid
        self.instances, self.drops = [], []
        self.Pg0AlreadyRunningError = _AlreadyRunning
        fake = self

        class Pg0:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                fake.instances.append(kwargs)

            def start(self):
                if fake.already_running:
                    raise _AlreadyRunning("instance already running")
                if fake.pidfile:
                    Path(self.kwargs["data_dir"], "postmaster.pid").write_text(
                        f"{fake.pid}\n{self.kwargs['data_dir']}\n0\n{fake.port}\n", encoding="utf-8")
                return types.SimpleNamespace(
                    pid=fake.info_pid, port=fake.port,
                    uri=f"postgresql://postgres:{self.kwargs['password']}@{fake.uri_host}:{fake.port}/postgres")

            def drop(self, force=True):
                fake.drops.append(self.kwargs["name"])

        self.Pg0 = Pg0


class FakeServerAuth:
    """Which superuser passwords the faked server accepts."""

    def __init__(self, *, honour_alter=True):
        self.accepted, self.statements, self.honour_alter = set(), [], honour_alter

    def connect(self, port, password, dbname="postgres"):
        if password not in self.accepted:
            raise _Refused("password authentication failed")
        auth = self

        class Connection:
            def execute(self, statement):
                text = repr(statement)
                auth.statements.append(text)
                match = re.search(r"Literal\('([0-9a-f]+)'\)", text)
                if auth.honour_alter and match:
                    auth.accepted = {match.group(1)}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return Connection()


class _Refused(Exception):
    pass


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    pg0 = FakePg0()
    auth = FakeServerAuth()
    monkeypatch.setitem(sys.modules, "pg0", pg0)
    real_pg0_init = pg0.Pg0.__init__

    def init(self, **kwargs):
        real_pg0_init(self, **kwargs)
        if "password" in kwargs:
            auth.accepted = {kwargs["password"]}

    monkeypatch.setattr(pg0.Pg0, "__init__", init)
    monkeypatch.setattr(postgres, "_connect", auth.connect)
    monkeypatch.setattr(postgres, "_refused", lambda port, password: password not in auth.accepted)
    return types.SimpleNamespace(pg0=pg0, auth=auth, parent=tmp_path)


def test_start_owns_a_fresh_loopback_cluster(fakes):
    server = postgres.start(fakes.parent, "pl-test-")
    assert server.owned and server.port == 55432 and server.pid == 4242 and server.host == "127.0.0.1"
    started = fakes.pg0.instances[0]
    assert started["config"] == {"listen_addresses": "127.0.0.1"}
    assert Path(started["data_dir"]).parent.parent == fakes.parent
    assert server.admin_conninfo() == "host=127.0.0.1 port=55432 user=postgres dbname=postgres"
    assert server.password not in server.admin_conninfo() and server.password not in repr(server)


def test_start_rotates_off_the_command_line_password(fakes):
    server = postgres.start(fakes.parent, "pl-test-")
    argv_password = fakes.pg0.instances[0]["password"]
    assert server.password != argv_password
    assert fakes.auth.accepted == {server.password}
    assert any(f"Literal('{server.password}')" in text for text in fakes.auth.statements)


def test_start_refuses_when_the_old_password_still_works(fakes):
    fakes.auth.honour_alter = False
    with pytest.raises(postgres.SetupError, match="still accepts the start-up password"):
        postgres.start(fakes.parent, "pl-test-")
    assert len(fakes.pg0.drops) == 1  # the owned server is stopped, not left running


def test_start_refuses_a_server_that_was_already_running(fakes):
    fakes.pg0.already_running = True
    with pytest.raises(postgres.SetupError, match="refusing to adopt it"):
        postgres.start(fakes.parent, "pl-test-")
    assert fakes.pg0.drops == []


@pytest.mark.parametrize("change", ["no_pidfile", "other_port", "other_pid"])
def test_start_refuses_a_server_outside_its_fresh_directory(fakes, change):
    if change == "no_pidfile":
        fakes.pg0.pidfile = False
    elif change == "other_port":
        fakes.pg0.port, fakes.pg0.pidfile = 55432, True
        original = fakes.pg0.Pg0.start

        def start(self):
            info = original(self)
            info.port = 55433
            return info

        fakes.pg0.Pg0.start = start
    else:
        fakes.pg0.info_pid = 999
    with pytest.raises(postgres.SetupError, match="refusing to adopt it"):
        postgres.start(fakes.parent, "pl-test-")
    assert fakes.pg0.drops == []


def test_start_refuses_a_non_loopback_endpoint(fakes):
    fakes.pg0.uri_host = "10.0.0.5"
    with pytest.raises(postgres.SetupError, match="loopback"):
        postgres.start(fakes.parent, "pl-test-")


@pytest.mark.parametrize("host", ["10.0.0.5", "0.0.0.0", "::1", "example.com", ""])
def test_loopback_host_refuses_everything_but_ipv4_loopback(host):
    with pytest.raises(postgres.SetupError):
        postgres.loopback_host(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "127.0.0.2"])
def test_loopback_host_accepts_ipv4_loopback(host):
    assert postgres.loopback_host(host).startswith("127.")


def test_stop_stops_only_the_server_it_started(fakes):
    server = postgres.start(fakes.parent, "pl-test-")
    assert server.stop() is True
    assert fakes.pg0.drops == [server.name]
    assert not server.data_dir.exists()
    assert server.stop() is False and fakes.pg0.drops == [server.name]


def test_stop_leaves_an_attached_server_running(fakes, tmp_path):
    pgdata = tmp_path / "pgdata"
    pgdata.mkdir()
    (pgdata / "postmaster.pid").write_text("4242\nx\n0\n55432\n", encoding="utf-8")
    server = postgres.Server(name="theirs", data_dir=tmp_path, pgdata=pgdata, port=55432, pid=4242,
                             password="x", owned=False)
    assert server.stop() is False
    assert fakes.pg0.drops == []


def test_stop_leaves_a_server_that_replaced_its_own(fakes):
    server = postgres.start(fakes.parent, "pl-test-")
    (server.pgdata / "postmaster.pid").write_text("5151\nx\n0\n55432\n", encoding="utf-8")
    assert server.stop() is False
    assert fakes.pg0.drops == []
    assert server.data_dir.exists()


def test_child_environment_drops_libpq_controls_and_the_daemon_url():
    server = postgres.Server(name="n", data_dir=Path("d"), pgdata=Path("d/p"), port=1, pid=1,
                             password="synthetic-secret", owned=False)
    env = postgres.child_environment(server, {"PATH": "p", "PGHOST": "h", "pgport": "1",
                                              "PGPASSFILE": "f", "PSEUDOLIFE_MCP_DATABASE_URL": "u"})
    assert env == {"PATH": "p", "PGPASSWORD": "synthetic-secret"}


def test_create_test_login_runs_the_native_client_without_the_password_on_argv(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(postgres.subprocess, "run",
                        lambda command, **kwargs: calls.append((command, kwargs)) or
                        subprocess.CompletedProcess(command, 0, "", ""))
    server = postgres.Server(name="n", data_dir=tmp_path, pgdata=tmp_path, port=55432, pid=1,
                             password="synthetic-secret", owned=True)
    login = tmp_path / "test-pg.env"
    postgres.create_test_login(Path("bin/pseudolife-stdio"), server, login)
    (command, kwargs), = calls
    assert command == [str(Path("bin/pseudolife-stdio")), "test-login", "create", "--rotate", "--json",
                       "--file", str(login), "--admin-url",
                       "host=127.0.0.1 port=55432 user=postgres dbname=postgres"]
    assert not any("synthetic-secret" in part for part in command)
    assert kwargs["env"]["PGPASSWORD"] == "synthetic-secret"


def test_the_pins_match_the_package_extras_they_replace():
    """rust/ci/requirements-postgres.txt carries the pins pyproject's extras
    bring today, so the package's deletion cannot loosen them."""
    pins = [line.strip() for line in (ROOT / "rust/ci/requirements-postgres.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert pins == ["pg0-embedded>=0.15.2,<0.16", "psycopg[binary]>=3.3.8"]
    for pin in pins:
        assert f'"{pin}"' in pyproject


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
        return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True}), "")

    monkeypatch.setattr(postgres.subprocess, "run", run)
    with package_blocked():
        module = importlib.import_module("rust.cli_harness.lease_ci")
        monkeypatch.setattr(module, "_DATABASE_TURN_MARKER", tmp_path / "no-turn-marker")
        yield types.SimpleNamespace(module=module, calls=calls, env_file=env_file, binary=binary,
                                    tmp=tmp_path, fakes=fakes)


def test_lease_fixture_exports_the_cluster_and_login(lease_ci, capsys):
    assert lease_ci.module.main() == 0
    lines = lease_ci.env_file.read_text(encoding="utf-8").splitlines()
    login = lease_ci.tmp / "runner-temp" / "test-pg.env"
    assert lines == ["PSEUDOLIFE_TEST_PG_HOST_PORT=127.0.0.1:55432", f"PSEUDOLIFE_TEST_PG_LOGIN_FILE={login}"]
    (command, kwargs), = lease_ci.calls
    assert command[:3] == [str(lease_ci.binary), "test-login", "create"]
    assert "PSEUDOLIFE_MCP_DATABASE_URL" not in kwargs["env"]
    # The cluster lives under the home directory, not RUNNER_TEMP (initdb's chmod).
    assert Path(lease_ci.fakes.pg0.instances[0]["data_dir"]).parent.parent == lease_ci.tmp


def test_lease_fixture_refuses_without_the_native_client(lease_ci, capsys):
    lease_ci.binary.unlink()
    assert lease_ci.module.main() == 1
    assert "native client is missing" in capsys.readouterr().err
    assert lease_ci.fakes.pg0.instances == [] and lease_ci.calls == []


def test_lease_fixture_reports_a_failed_login_without_its_secret(lease_ci, monkeypatch, capsys):
    def run(command, **kwargs):
        secret = kwargs["env"]["PGPASSWORD"]
        return subprocess.CompletedProcess(command, 3, "", f"refused: password={secret}")

    monkeypatch.setattr(postgres.subprocess, "run", run)
    assert lease_ci.module.main() == 3
    err = capsys.readouterr().err
    assert "test-login fixture provisioning failed (exit 3)" in err
    assert not lease_ci.env_file.exists()
    assert not re.search(r"password=[0-9a-f]{32}", err)


def test_lease_fixture_imports_nothing_from_the_package(lease_ci):
    assert lease_ci.module.main() == 0
    assert not any(name.startswith("pseudolife_memory") for name in sys.modules)
