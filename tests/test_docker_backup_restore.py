"""Clone-free Docker backup and restore; every Docker call is replaced."""
import gzip
import io
import json
import sys
import tarfile
import types
from pathlib import Path

import pytest

from pseudolife_memory import backup_cli, cli, update_cli as up


@pytest.mark.parametrize("configured", [up.DAEMON_CONTAINER, up.PG_CONTAINER])
def test_backup_command_detects_docker_without_a_clone(tmp_path, monkeypatch, capsys, configured):
    called = []
    monkeypatch.setattr(up, "which", lambda _: "docker")
    monkeypatch.setattr(up, "run_cli", lambda argv, **kw:
                        (0, "container-id") if argv[-1] == configured else (1, "No such container"))
    monkeypatch.setattr(up, "data_dir", lambda: tmp_path / "client-data")
    fake = types.ModuleType("pseudolife_memory.docker_backup_restore")

    class Bank:
        def __init__(self, **kwargs):
            pass

        def backup(self, **kwargs):
            called.append(kwargs)
            return {"dump": tmp_path / "dump.sql.gz", "state": tmp_path / "state.tgz", "pruned": []}

    fake.DockerBank = Bank
    monkeypatch.setitem(sys.modules, fake.__name__, fake)
    monkeypatch.setattr(backup_cli, "perform_backup", lambda *a, **kw: pytest.fail("Docker must not back up pip state"))
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "backup", "--out", str(tmp_path)])
    backup_cli.run_backup()
    assert called and called[0]["out"] == tmp_path
    assert "dump.sql.gz" in capsys.readouterr().out


@pytest.mark.parametrize("selector,value", [
    ("--container", "pg"), ("--daemon-container", "daemon"),
    ("--db", "bank"), ("--user", "role"), ("--db-port", "5433"),
])
def test_explicit_docker_selectors_do_not_fall_back_to_file_mode(tmp_path, monkeypatch, selector, value):
    from pseudolife_memory import docker_backup_restore as module
    seen = []
    class Bank:
        def __init__(self, **kwargs):
            seen.append(kwargs)
            raise up.UpdateError("Docker unavailable; start Docker and retry", 2)
    monkeypatch.setattr(module, "DockerBank", Bank)
    monkeypatch.setattr(up, "run_cli", lambda *a, **kw: pytest.fail("explicit selectors do not need auto-detection"))
    monkeypatch.setattr(backup_cli, "perform_backup", lambda *a, **kw: pytest.fail("explicit Docker must not fall back"))
    monkeypatch.setattr(up, "data_dir", lambda: tmp_path / "client-data")
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "backup", "--data-dir", str(tmp_path), selector, value])
    with pytest.raises(SystemExit) as exc:
        backup_cli.run_backup()
    assert exc.value.code == 2
    assert len(seen) == 1


def test_restore_command_dispatches_without_a_clone(monkeypatch):
    fake = types.ModuleType("pseudolife_memory.restore_cli")
    seen = []
    fake.main = lambda argv: seen.append(argv) or 0
    monkeypatch.setitem(sys.modules, fake.__name__, fake)
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "restore", "--backup-file", "bank.sql.gz", "--yes"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0 and seen == [["--backup-file", "bank.sql.gz", "--yes"]]


def _state_bytes(name="config.yaml", content=b"memory: {}\n"):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


class DockerWorld:
    def __init__(self):
        self.calls = []
        self.helpers = {}
        self.fail_state = False
        self.fail_replay = False
        self.complete = True
        self.database_exists = True
        self.pg_name = up.PG_CONTAINER
        self.daemon = {"Image": "sha256:local", "State": {"Running": True},
                       "HostConfig": {"PortBindings": {"8765/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18765"}]}},
                       "NetworkSettings": {"Networks": {"fixture": {}}},
                       "Mounts": [{"Destination": "/data", "Name": "legacy-state"}],
                       "Config": {"Env": ["PSEUDOLIFE_MCP_DATABASE_URL=postgresql://custom_role:fixture@db/custom_bank"]}}
        self.pg = {"State": {"Running": True}, "Config": {"Env": ["POSTGRES_USER=custom_role", "POSTGRES_DB=custom_bank"]},
                   "NetworkSettings": {"Networks": {"fixture": {"Aliases": ["db"]}}}}

    def run(self, argv, **kwargs):
        args = list(map(str, argv[1:]))
        self.calls.append(args)
        if args[0] == "exec" and "df" in args:
            return 0, "Filesystem 1024-blocks Used Available Capacity Mounted on\noverlay 99999999 1 99999998 1% /\n"
        if args[0] == "inspect":
            return 0, json.dumps([self.pg if args[1] == self.pg_name else self.daemon])
        if args[0] == "create":
            helper = f"helper-{len(self.helpers)}"
            self.helpers[helper] = args[-1]
            return 0, helper
        if args[0] == "exec" and "pg_database" in " ".join(args):
            return 0, "t" if self.database_exists else "f"
        if args[0] == "exec" and "pg_dump" in " ".join(args) and not self.database_exists:
            return 1, "database absent"
        if args[:2] == ["start", "-a"]:
            script = self.helpers[args[-1]]
            if "df -Pk" in script:
                return 0, "overlay 99999999 1 99999998 1% /data\n"
            return (1 if self.fail_state and "tar czf" in script else 0), ""
        if args[0] == "cp" and ":" in args[1] and not Path(args[1]).is_file():
            target = Path(args[2])
            if args[1].startswith(self.pg_name + ":"):
                target.write_bytes(gzip.compress(b"-- PostgreSQL database dump complete\n" if self.complete else b"SET x;\n"))
            else:
                target.write_bytes(_state_bytes())
        if args[0] == "exec" and "-f" in args and "psql" in args and self.fail_replay:
            return 1, "private diagnostic"
        return 0, ""


@pytest.fixture
def docker_bank(tmp_path, monkeypatch):
    from pseudolife_memory import docker_backup_restore as module
    from pseudolife_memory.docker_backup_restore import DockerBank
    world = DockerWorld()
    world.original_run_cli = up.run_cli
    monkeypatch.setattr(up, "run_cli", world.run)
    monkeypatch.setattr(module, "copy_from_container", lambda docker, source, target: world.run([docker, "cp", source, str(target)]))
    monkeypatch.setattr(module, "copy_into_container", lambda docker, source, container, remote: world.run(
        [docker, "exec", "-i", container, "sh", "-c", module.STREAM_INTO, "_", remote, "<", str(source)]))
    monkeypatch.setattr(up, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(up, "data_dir", lambda: tmp_path / "clone" / "data")
    monkeypatch.setattr(up, "fetch_json", lambda *a, **kw: {"status": "ok", "ready": True})
    monkeypatch.setattr(up, "sleep", lambda _: None)
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / ".git").write_text("gitdir: fixture\n")
    return DockerBank(log=lambda _: None), world


def _input_dump(tmp_path):
    path = tmp_path / "input.sql.gz"
    path.write_bytes(gzip.compress(b"-- PostgreSQL database dump complete\n"))
    return path


@pytest.mark.parametrize("action", ["backup", "restore"])
def test_low_disk_refuses_docker_backup_and_restore_before_artifacts_or_stop(docker_bank, tmp_path, monkeypatch, action):
    from pseudolife_memory import docker_backup_restore as module
    bank, world = docker_bank
    monkeypatch.setattr(module.shutil, "disk_usage", lambda _: types.SimpleNamespace(free=8 * 1024**3 - 1))
    with pytest.raises(up.UpdateError, match="8 GiB") as exc:
        if action == "backup":
            bank.backup(out=tmp_path / "artifacts")
        else:
            bank.restore(_input_dump(tmp_path), leave_stopped=True, safety_dir=tmp_path / "safety")
    destination = tmp_path / ("artifacts" if action == "backup" else "safety")
    assert str(destination.resolve()) in str(exc.value)
    assert "free at least 8 GiB there" in str(exc.value)
    assert not any(c[0] in ("stop", "create", "cp") or "pg_dump" in " ".join(c) for c in world.calls)
    assert not (tmp_path / "artifacts").exists() and not (tmp_path / "safety").exists()


@pytest.mark.parametrize("location", ["storage", "pg-tmp"])
@pytest.mark.parametrize("database_exists", [True, False])
def test_low_docker_disk_refuses_restore_before_stopping_the_daemon(docker_bank, tmp_path, monkeypatch, location, database_exists):
    bank, world = docker_bank
    world.database_exists = database_exists
    original = world.run

    def low_disk(argv, **kwargs):
        if "df" in argv and argv[-1] == ("/" if location == "storage" else "/tmp"):
            return 0, "Filesystem 1024-blocks Used Available Capacity Mounted on\noverlay 999 998 1 99% /\n"
        return original(argv, **kwargs)

    monkeypatch.setattr(up, "run_cli", low_disk)
    with pytest.raises(up.UpdateError, match="8 GiB"):
        bank.restore(_input_dump(tmp_path), leave_stopped=True, safety_dir=tmp_path / "safety")
    assert not any(c[0] in ("stop", "create", "cp") or "pg_dump" in " ".join(c) for c in world.calls)
    assert not (tmp_path / "safety").exists()


def test_the_exact_disk_floor_allows_a_clone_free_backup(docker_bank, tmp_path, monkeypatch):
    from pseudolife_memory import disk_space
    bank, _world = docker_bank
    monkeypatch.setattr(disk_space.shutil, "disk_usage", lambda _: types.SimpleNamespace(free=disk_space.MIN_FREE))
    result = bank.backup(out=tmp_path / "artifacts")
    assert result["dump"].is_file() and result["state"].is_file()


@pytest.mark.parametrize("action", ["backup", "restore"])
def test_small_system_temp_does_not_refuse_ample_backup_destination(docker_bank, tmp_path, monkeypatch, action):
    from pseudolife_memory import disk_space, docker_backup_restore as module
    bank, _world = docker_bank
    small = tmp_path / "small-temp"
    small.mkdir()
    measured = []

    def usage(path):
        measured.append(Path(path))
        return types.SimpleNamespace(free=1 if Path(path) == small else disk_space.MIN_FREE)

    monkeypatch.setattr(module.tempfile, "gettempdir", lambda: str(small))
    monkeypatch.setattr(disk_space.shutil, "disk_usage", usage)
    if action == "backup":
        result = bank.backup(out=tmp_path / "destination")
        assert result["dump"].is_file() and result["state"].is_file()
    else:
        result = bank.restore(_input_dump(tmp_path), leave_stopped=True, safety_dir=tmp_path / "destination")
        assert result["safety_dump"].is_file() and result["safety_state"].is_file()
    assert small not in measured


def test_backup_does_not_reapply_initial_floor_after_its_own_dump(tmp_path, monkeypatch):
    from pseudolife_memory import disk_space, docker_backup_restore as module
    world = DockerWorld()
    monkeypatch.setattr(up, "which", lambda _: "docker")
    monkeypatch.setattr(up, "run_cli", world.run)
    free = {"bytes": 9 * 1024**3}
    measured = []

    def usage(path):
        measured.append(free["bytes"])
        return types.SimpleNamespace(free=free["bytes"])

    def docker_tar(argv, *, stdout, **kwargs):
        payload = (gzip.compress(b"-- PostgreSQL database dump complete\n")
                   if argv[-2].startswith(world.pg_name + ":") else _state_bytes())
        with tarfile.open(fileobj=stdout, mode="w") as archive:
            entry = tarfile.TarInfo("artifact")
            entry.size = len(payload)
            archive.addfile(entry, io.BytesIO(payload))
        return types.SimpleNamespace(returncode=0)

    original_copy = module.copy_from_container

    def copy(docker, source, target):
        original_copy(docker, source, target)
        if source.startswith(world.pg_name + ":"):
            # A successful 1.5 GiB dump leaves 7.5 GiB for the small state
            # archive; that does not invalidate admission at 9 GiB.
            free["bytes"] = 15 * 1024**3 // 2

    monkeypatch.setattr(disk_space.shutil, "disk_usage", usage)
    monkeypatch.setattr(module.subprocess, "run", docker_tar)
    monkeypatch.setattr(module, "copy_from_container", copy)
    bank = module.DockerBank(log=lambda _: None)
    result = bank.backup(out=tmp_path / "destination")
    assert result["dump"].is_file() and result["state"].is_file()
    assert measured == [9 * 1024**3]


@pytest.mark.parametrize("location", ["/var/lib/postgresql/legacy", "/data", "/data/nested", None])
def test_restore_checks_separate_destination_mounts_before_stop(docker_bank, tmp_path, monkeypatch, location):
    bank, world = docker_bank
    world.pg["Mounts"] = [{"Destination": "/var/lib/postgresql/legacy", "Name": "legacy-pg", "RW": True}]
    world.pg["Mounts"].append({"Destination": "/readonly", "Name": "settings", "RW": False})
    bank.running["Mounts"].append({"Destination": "/data/nested", "Name": "legacy-nested"})
    original = world.run
    def run(argv, **kw):
        if "df" in argv and argv[-1] == "/readonly":
            pytest.fail("read-only configuration is not a restore destination")
        if "df" in argv and argv[-1] == location:
            return 0, "overlay 99999999 1 1 99% /\n"
        if argv[1:3] == ["start", "-a"] and "df -Pk" in world.helpers.get(argv[-1], ""):
            script = world.helpers[argv[-1]]
            return 0, "\n".join("overlay 99999999 1 " + ("1" if path == location else "8388608") + " 1% " + path
                                for path in ["/data", "/data/nested"] if path in script)
        return original(argv, **kw)
    monkeypatch.setattr(up, "run_cli", run)
    state_file = tmp_path / "state.tgz"
    state_file.write_bytes(_state_bytes())
    if location is None:
        bank.restore(_input_dump(tmp_path), state_archive=state_file, leave_stopped=True)
        assert any("DROP DATABASE" in str(call) for call in world.calls)
        return
    with pytest.raises(up.UpdateError, match="8 GiB"):
        bank.restore(_input_dump(tmp_path), state_archive=state_file, leave_stopped=True)
    assert not any(call[0] == "stop" or "DROP DATABASE" in str(call) for call in world.calls)


@pytest.mark.parametrize("contains_bank_data", [False, True])
def test_socket_only_tmpfs_is_not_a_restore_destination(docker_bank, tmp_path, monkeypatch, contains_bank_data):
    bank, world = docker_bank
    socket = "/var/run/postgresql"
    world.pg["Mounts"] = [
        {"Type": "volume", "Destination": "/legacy-data", "RW": True},
        {"Type": "volume", "Destination": "/tablespaces", "RW": True},
        {"Type": "tmpfs", "Destination": socket, "RW": True}]
    original = world.run

    def run(argv, **kw):
        if "df" in argv and argv[-1] == socket:
            return 0, "tmpfs 65536 1 65535 1% /var/run/postgresql\n"
        if argv[1:2] == ["exec"] and any("find " in str(value) for value in argv):
            return 0, "bank-data\n" if contains_bank_data else ""
        return original(argv, **kw)

    monkeypatch.setattr(up, "run_cli", run)
    if contains_bank_data:
        with pytest.raises(up.UpdateError, match="8 GiB"):
            bank._restore_space(tmp_path / "safety", state=False)
    else:
        bank._restore_space(tmp_path / "safety", state=False)
    assert not any(call[0] in ("stop", "create", "cp") for call in world.calls)


@pytest.mark.parametrize("phase", ["backup", "staging", "state-staging"])
def test_restore_rechecks_space_after_safety_backup_and_staging(docker_bank, tmp_path, monkeypatch, phase):
    from pseudolife_memory import disk_space
    bank, world = docker_bank
    low = {"now": False}
    original = world.run
    def run(argv, **kw):
        result = original(argv, **kw)
        if phase == "staging" and "gunzip -c" in str(argv):
            low["now"] = True
        if phase == "state-staging" and argv[1:2] == ["cp"] and str(argv[-1]).endswith(":/tmp/pl_state.tgz"):
            low["now"] = True
        return result
    monkeypatch.setattr(up, "run_cli", run)
    original_backup = bank.backup
    def backup(**kw):
        result = original_backup(**kw)
        if phase == "backup":
            low["now"] = True
        return result
    monkeypatch.setattr(bank, "backup", backup)
    monkeypatch.setattr(disk_space.shutil, "disk_usage", lambda _: types.SimpleNamespace(free=disk_space.MIN_FREE - int(low["now"])))
    state_file = tmp_path / "state.tgz"
    state_file.write_bytes(_state_bytes())
    with pytest.raises(up.UpdateError, match="8 GiB"):
        bank.restore(_input_dump(tmp_path), state_archive=state_file, leave_stopped=True)
    assert not any("DROP DATABASE" in str(call) for call in world.calls)
    assert ["start", bank.daemon] in world.calls


def test_docker_backup_uses_actual_role_and_database_and_stopped_state(docker_bank, tmp_path):
    bank, world = docker_bank
    world.daemon["State"]["Running"] = False
    result = bank.backup(out=tmp_path / "artifacts")
    assert result["dump"].is_file() and result["state"].is_file()
    dump = next(c for c in world.calls if "pg_dump" in " ".join(c))
    from psycopg.conninfo import conninfo_to_dict
    assert dump[-3] == "custom_role" and conninfo_to_dict(dump[-2]) == {"dbname": "custom_bank"}
    create = next(c for c in world.calls if c[0] == "create")
    assert create[create.index("--volumes-from") + 1] == up.DAEMON_CONTAINER + ":ro"
    assert not any(c == ["start", up.DAEMON_CONTAINER] for c in world.calls)
    assert any(c[0] == "rm" and c[-1].startswith("helper-") for c in world.calls)


def test_docker_backup_treats_a_conninfo_shaped_database_name_as_a_literal(docker_bank, tmp_path):
    from psycopg.conninfo import conninfo_to_dict
    bank, world = docker_bank
    bank.db = "host=elsewhere"
    bank.backup(out=tmp_path / "artifacts")
    dump = next(c for c in world.calls if "pg_dump" in " ".join(c))
    selected = conninfo_to_dict(dump[-2])
    assert selected.get("dbname") == bank.db and "host" not in selected


def test_restore_treats_a_conninfo_shaped_database_name_as_a_literal(docker_bank, tmp_path):
    from psycopg.conninfo import conninfo_to_dict
    bank, world = docker_bank
    bank.db = "host=elsewhere"
    bank.restore(_input_dump(tmp_path), leave_stopped=True)
    replay = next(c for c in world.calls if "psql" in c and "-f" in c)
    selected = conninfo_to_dict(replay[replay.index("-d") + 1])
    assert selected == {"dbname": bank.db}
    drop = next(c[-1] for c in world.calls if "DROP DATABASE" in " ".join(c))
    assert '"host=elsewhere"' in drop


@pytest.mark.parametrize("name", ["bank with spaces", "bank-name", "bank'quote"])
def test_restore_keeps_legitimate_literal_database_names(docker_bank, tmp_path, name):
    from psycopg.conninfo import conninfo_to_dict
    bank, world = docker_bank
    bank.db = name
    bank.restore(_input_dump(tmp_path), leave_stopped=True)
    replay = next(c for c in world.calls if "psql" in c and "-f" in c)
    assert conninfo_to_dict(replay[replay.index("-d") + 1]) == {"dbname": name}


def test_missing_postgres_socket_names_the_database_port_fix(docker_bank, monkeypatch):
    bank, world = docker_bank
    original = world.run

    def missing_socket(argv, **kwargs):
        if "psql" in argv:
            return 2, "psql: connection to server on socket failed: No such file or directory"
        return original(argv, **kwargs)

    monkeypatch.setattr(up, "run_cli", missing_socket)
    with pytest.raises(up.UpdateError, match="--db-port"):
        bank.database_exists()


def test_missing_socket_advice_reads_real_command_stderr(docker_bank, monkeypatch):
    import subprocess
    bank, world = docker_bank
    monkeypatch.setattr(up, "run_cli", world.original_run_cli)
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 2, stdout="", stderr="psql: connection to server on socket failed: No such file or directory"))
    with pytest.raises(up.UpdateError, match="--db-port"):
        bank.database_exists()


def test_backup_missing_socket_names_the_same_port_fix(docker_bank, monkeypatch, tmp_path):
    bank, world = docker_bank
    original = world.run

    def missing_socket(argv, **kwargs):
        if any("pg_dump" in str(value) for value in argv):
            return 1, "pg_dump: connection to server on socket failed: No such file or directory"
        return original(argv, **kwargs)

    monkeypatch.setattr(up, "run_cli", missing_socket)
    with pytest.raises(up.UpdateError, match="--db-port"):
        bank.backup(out=tmp_path / "artifacts")


def test_successful_postgres_queries_keep_stderr_out_of_data(docker_bank, monkeypatch):
    import subprocess
    bank, world = docker_bank
    monkeypatch.setattr(up, "run_cli", world.original_run_cli)
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout="t\n", stderr="a server warning\n"))
    assert bank.database_exists()


def test_restore_binds_every_postgres_client_to_the_selected_local_container(docker_bank, tmp_path):
    bank, world = docker_bank
    world.pg["Config"]["Env"] += ["PGHOSTADDR=10.0.0.2", "PGSERVICE=unrelated", "PGSERVICEFILE=/tmp/profile"]
    bank.restore(_input_dump(tmp_path), leave_stopped=True)
    clients = [c for c in world.calls if "psql" in c]
    assert clients
    for command in clients:
        assert command[command.index("-h") + 1].startswith("/")
        assert command[command.index("-p") + 1] == "5432"
        assert all(name in command for name in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"))
    dump = next(c for c in world.calls if "pg_dump" in " ".join(c))
    assert "unset PGHOSTADDR PGSERVICE PGSERVICEFILE" in " ".join(dump)


def test_database_port_follows_runtime_connection_and_explicit_override(docker_bank):
    from pseudolife_memory.docker_backup_restore import DockerBank
    _bank, world = docker_bank
    world.daemon["Config"]["Env"] = ["PSEUDOLIFE_MCP_DATABASE_URL=postgresql://custom_role@db:6543/custom_bank"]
    for requested, expected in [(None, 6543), (7654, 7654)]:
        bank = DockerBank(db_port=requested, log=lambda _: None)
        assert bank.database_exists()
        command = next(c for c in reversed(world.calls) if "psql" in c)
        assert command[command.index("-p") + 1] == str(expected)


@pytest.mark.parametrize("runtime_port,expected", [(None, 5432), ("6543", 6543)])
def test_postgres_client_environment_does_not_override_daemon_port(docker_bank, runtime_port, expected):
    from pseudolife_memory.docker_backup_restore import DockerBank
    _bank, world = docker_bank
    world.pg["Config"]["Env"].append("PGPORT=7654")
    world.daemon["Config"]["Env"] = ["PSEUDOLIFE_MCP_DATABASE_URL=postgresql://custom_role@db/custom_bank"]
    if runtime_port is not None:
        world.daemon["Config"]["Env"].append("PGPORT=" + runtime_port)
    bank = DockerBank(log=lambda _: None)
    assert bank.database_exists()
    command = next(c for c in reversed(world.calls) if "psql" in c)
    assert command[command.index("-p") + 1] == str(expected)


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_invalid_database_port_refuses_before_writing(docker_bank, port):
    from pseudolife_memory.docker_backup_restore import DockerBank
    _bank, world = docker_bank
    with pytest.raises(up.UpdateError, match="--db-port"):
        DockerBank(db_port=port, log=lambda _: None)
    assert not any(c[0] in ("stop", "create", "cp") for c in world.calls)


@pytest.mark.parametrize("failure", ["dump", "state"])
def test_docker_backup_requires_both_artifacts_and_removes_partial_files(docker_bank, tmp_path, failure):
    bank, world = docker_bank
    world.complete = failure != "dump"
    world.fail_state = failure == "state"
    with pytest.raises(up.UpdateError):
        bank.backup(out=tmp_path / "artifacts")
    assert list((tmp_path / "artifacts").iterdir()) == []


def test_restore_safety_pair_is_outside_clone_and_precedes_drop(docker_bank, tmp_path):
    bank, world = docker_bank
    state = tmp_path / "input.tgz"
    state.write_bytes(_state_bytes())
    result = bank.restore(_input_dump(tmp_path), state_archive=state, leave_stopped=True)
    assert result["safety_dump"].is_relative_to(tmp_path / "home")
    assert result["safety_dump"].exists() and result["safety_state"].exists()
    drop = next(i for i, c in enumerate(world.calls) if "DROP DATABASE" in " ".join(c))
    copies = [i for i, c in enumerate(world.calls) if c[0] == "cp" and c[1].startswith("helper-")]
    assert copies and max(copies) < drop
    assert ["start", up.DAEMON_CONTAINER] not in world.calls
    restore = next(c[-1] for c in world.calls if c[0] == "create" and "find /data" in c[-1])
    assert "! -name move.json" in restore and "--exclude=move.json" in restore


def test_failed_safety_archive_restarts_original_and_never_drops(docker_bank, tmp_path):
    bank, world = docker_bank
    world.fail_state = True
    with pytest.raises(up.UpdateError):
        bank.restore(_input_dump(tmp_path))
    assert not any("DROP DATABASE" in " ".join(c) for c in world.calls)
    assert ["start", up.DAEMON_CONTAINER] in world.calls


def test_failed_replay_leaves_daemon_stopped_and_names_safety_restore(docker_bank, tmp_path):
    bank, world = docker_bank
    world.fail_replay = True
    with pytest.raises(up.UpdateError, match="pseudolife-mcp restore --backup-file"):
        bank.restore(_input_dump(tmp_path))
    assert ["start", up.DAEMON_CONTAINER] not in world.calls


def _interrupt_at(world, marker, *, again=None):
    """Raise KeyboardInterrupt from the Docker call whose argv holds
    ``marker``, and optionally from the next call holding ``again``."""
    original = world.run
    fired = set()

    def run(argv, **kwargs):
        joined = " ".join(map(str, argv))
        for key in (marker, again):
            if key and key in joined and key not in fired and (key == marker or marker in fired):
                fired.add(key)
                world.calls.append(list(map(str, argv[1:])))
                raise KeyboardInterrupt
        return original(argv, **kwargs)
    return run


@pytest.mark.parametrize("phase", ["replay", "drop"])
def test_interrupt_after_the_drop_began_prints_the_recovery_command(docker_bank, tmp_path, monkeypatch, capsys, phase):
    bank, world = docker_bank
    marker = "ON_ERROR_STOP=1 -f" if phase == "replay" else "DROP DATABASE"
    monkeypatch.setattr(up, "run_cli", _interrupt_at(world, marker))
    with pytest.raises(KeyboardInterrupt):
        bank.restore(_input_dump(tmp_path))
    err = capsys.readouterr().err
    safety = tmp_path / "home" / ".pseudolife-mcp" / "backups"
    dump = next(safety.glob("pseudolife_memory-*.sql.gz"))
    assert err.startswith("restore failed: restore failed after database replacement began (KeyboardInterrupt); "
                          "the daemon is left stopped. Recover with pseudolife-mcp restore --backup-file ")
    assert str(dump) in err and "--yes" in err
    assert ["stop", up.DAEMON_CONTAINER] in world.calls[-6:]
    assert ["start", up.DAEMON_CONTAINER] not in world.calls


def test_a_second_interrupt_during_the_stop_still_prints_the_recovery(docker_bank, tmp_path, monkeypatch, capsys):
    bank, world = docker_bank
    stop = "stop " + up.DAEMON_CONTAINER
    monkeypatch.setattr(up, "run_cli", _interrupt_at(world, "DROP DATABASE", again=stop))
    with pytest.raises(KeyboardInterrupt):
        bank.restore(_input_dump(tmp_path))
    err = capsys.readouterr().err
    assert err.startswith("restore failed: restore failed after database replacement began (KeyboardInterrupt); "
                          f"run docker stop {up.DAEMON_CONTAINER} first. Recover with pseudolife-mcp restore ")


def test_interrupt_before_the_drop_restarts_the_daemon_without_recovery_text(docker_bank, tmp_path, monkeypatch, capsys):
    bank, world = docker_bank
    monkeypatch.setattr(up, "run_cli", _interrupt_at(world, "gunzip -c"))
    with pytest.raises(KeyboardInterrupt):
        bank.restore(_input_dump(tmp_path))
    assert capsys.readouterr().err == ""
    assert ["start", up.DAEMON_CONTAINER] in world.calls
    assert not any("DROP DATABASE" in " ".join(c) for c in world.calls)


@pytest.mark.parametrize("answer,expected_calls", [("n", 0), ("y", 1)])
def test_restore_asks_once_before_overwrite(tmp_path, monkeypatch, answer, expected_calls):
    from pseudolife_memory import restore_cli
    prompts, calls = [], []
    monkeypatch.setattr(up, "interactive", lambda: True)
    monkeypatch.setattr(up, "ask", lambda question: prompts.append(question) or answer)
    monkeypatch.setattr(up, "data_dir", lambda: tmp_path / "client-data")

    class Bank:
        def __init__(self, **kw):
            pass

        def restore(self, *a, **kw):
            calls.append(kw)

    monkeypatch.setattr(restore_cli, "DockerBank", Bank)
    assert restore_cli.main(["--backup-file", str(_input_dump(tmp_path))]) == 0
    assert len(prompts) == 1 and "[y/N]" in prompts[0]
    assert len(calls) == expected_calls


def test_unattended_restore_needs_yes_before_docker(tmp_path, monkeypatch):
    from pseudolife_memory import restore_cli
    monkeypatch.setattr(up, "interactive", lambda: False)
    monkeypatch.setattr(up, "run_cli", lambda *a, **kw: pytest.fail("no Docker mutation without confirmation"))
    assert restore_cli.main(["--backup-file", str(_input_dump(tmp_path))]) == 2


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows' CRT isatty() is true for the null device")
def test_a_null_device_is_not_a_person_at_a_terminal(tmp_path, monkeypatch, capsys):
    import os
    from pseudolife_memory import restore_cli
    with open(os.devnull, encoding="utf-8") as stdin, open(os.devnull, "w", encoding="utf-8") as stdout:
        assert stdin.isatty() and stdout.isatty()  # what the old check read as a terminal
        monkeypatch.setattr(sys, "stdin", stdin)
        monkeypatch.setattr(sys, "stdout", stdout)
        assert up.interactive() is False
        monkeypatch.setattr(up, "ask", lambda question: pytest.fail("no prompt without a console"))
        monkeypatch.setattr(up, "run_cli", lambda *a, **kw: pytest.fail("no Docker call without confirmation"))
        assert restore_cli.main(["--backup-file", str(_input_dump(tmp_path))]) == 2
    assert capsys.readouterr().err.strip() == (
        "restore failed: restore replaces the existing bank; rerun with --yes to confirm. Nothing was restored")


def test_interactive_needs_both_streams_to_be_terminals(monkeypatch):
    class Stream:
        def __init__(self, tty):
            self.tty = tty

        def isatty(self):
            return self.tty

        def fileno(self):
            raise ValueError("no descriptor")  # a console check must fail closed

    for stdin, stdout in ((False, True), (True, False)):
        monkeypatch.setattr(sys, "stdin", Stream(stdin))
        monkeypatch.setattr(sys, "stdout", Stream(stdout))
        assert up.interactive() is False
    monkeypatch.setattr(sys, "stdin", Stream(True))
    monkeypatch.setattr(sys, "stdout", Stream(True))
    assert up.interactive() is (sys.platform != "win32")


@pytest.mark.parametrize("name", ["../escape", "/absolute", "dir/../../escape"])
def test_unsafe_state_refuses_before_stopping(docker_bank, tmp_path, name):
    bank, world = docker_bank
    state = tmp_path / "unsafe.tgz"
    state.write_bytes(_state_bytes(name))
    before = len(world.calls)
    with pytest.raises(up.UpdateError, match="unsafe"):
        bank.restore(_input_dump(tmp_path), state_archive=state)
    assert len(world.calls) == before


def test_backup_copy_creates_host_owned_file_instead_of_extracting_container_ownership(tmp_path, monkeypatch):
    from pseudolife_memory import disk_space, docker_backup_restore as module
    monkeypatch.setattr(disk_space.shutil, "disk_usage", lambda _: types.SimpleNamespace(free=disk_space.MIN_FREE))

    def docker_tar(argv, *, stdout, **kwargs):
        assert argv[-1] == "-"
        with tarfile.open(fileobj=stdout, mode="w") as archive:
            entry = tarfile.TarInfo("root-owned.sql.gz")
            entry.uid = 0
            entry.gid = 0
            entry.size = 7
            archive.addfile(entry, io.BytesIO(b"payload"))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", docker_tar)
    target = tmp_path / "dump.part"
    module.copy_from_container("docker", "pg:/tmp/dump", target)
    assert target.read_bytes() == b"payload"


@pytest.mark.parametrize("outcome", ["success", "failure", "interrupt"])
def test_backup_stages_on_destination_and_closes_stream(tmp_path, monkeypatch, outcome):
    from pseudolife_memory import disk_space, docker_backup_restore as module
    monkeypatch.setattr(disk_space.shutil, "disk_usage", lambda _: types.SimpleNamespace(free=disk_space.MIN_FREE))
    temporary_file = module.tempfile.TemporaryFile
    streams = []
    target = tmp_path / "destination" / "dump.part"
    target.parent.mkdir()

    def stage(*args, **kwargs):
        assert Path(kwargs["dir"]) == target.parent
        stream = temporary_file(*args, **kwargs)
        streams.append(stream)
        return stream

    def docker_tar(argv, *, stdout, **kwargs):
        if outcome == "interrupt":
            raise KeyboardInterrupt
        with tarfile.open(fileobj=stdout, mode="w") as archive:
            entry = tarfile.TarInfo("dump")
            entry.size = 7
            archive.addfile(entry, io.BytesIO(b"payload"))
        return types.SimpleNamespace(returncode=int(outcome == "failure"))

    monkeypatch.setattr(module.tempfile, "TemporaryFile", stage)
    monkeypatch.setattr(module.subprocess, "run", docker_tar)
    if outcome == "success":
        module.copy_from_container("docker", "pg:/tmp/dump", target)
        assert target.read_bytes() == b"payload"
    else:
        error = KeyboardInterrupt if outcome == "interrupt" else up.UpdateError
        with pytest.raises(error):
            module.copy_from_container("docker", "pg:/tmp/dump", target)
        assert not target.exists()
    assert streams and all(stream.closed for stream in streams)
    assert list(target.parent.iterdir()) == ([target] if outcome == "success" else [])


def test_unknown_database_host_requires_all_explicit_selectors(docker_bank):
    from pseudolife_memory.docker_backup_restore import DockerBank
    _, world = docker_bank
    world.daemon["Config"]["Env"] = ["PSEUDOLIFE_MCP_DATABASE_URL=postgresql://custom_role:fixture@other-db/custom_bank"]
    with pytest.raises(up.UpdateError, match="--container"):
        DockerBank()
    with pytest.raises(up.UpdateError):
        DockerBank(container=up.PG_CONTAINER)
    bank = DockerBank(container=up.PG_CONTAINER, db="custom_bank", user="custom_role", db_port=5432)
    assert bank.db == "custom_bank" and bank.user == "custom_role"
    assert all(c[0] == "inspect" for c in world.calls)


def test_escaping_state_link_is_rejected_before_mutation(docker_bank, tmp_path):
    bank, world = docker_bank
    state = tmp_path / "unsafe.tgz"
    with tarfile.open(state, "w:gz") as archive:
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../escape"
        archive.addfile(link)
    before = len(world.calls)
    with pytest.raises(up.UpdateError, match="unsafe"):
        bank.restore(_input_dump(tmp_path), state_archive=state)
    assert len(world.calls) == before


def test_docker_rotation_keeps_legacy_backups_and_three_newest(docker_bank, tmp_path):
    import os
    bank, _ = docker_bank
    out = tmp_path / "backups"
    out.mkdir()
    for index in range(6):
        for prefix, suffix in (("pseudolife_memory", "sql.gz"), ("pseudolife_state", "tgz")):
            path = out / f"{prefix}-20200101-000000-{index:08x}.{suffix}"
            path.write_bytes(b"old")
            os.utime(path, (0, 0))
    legacy = out / "pseudolife_memory-20000101-000000.sql.gz"
    legacy.write_bytes(b"legacy")
    result = bank.backup(out=out)
    assert len(result["pruned"]) == 8 and legacy.exists()
    assert len(list(out.glob("*.tgz"))) == 3


def test_yes_and_move_selectors_skip_prompt_and_leave_stopped(tmp_path, monkeypatch):
    from pseudolife_memory import restore_cli
    seen = {}
    monkeypatch.setattr(up, "ask", lambda _: pytest.fail("--yes must not prompt"))
    monkeypatch.setattr(up, "data_dir", lambda: tmp_path / "client-data")

    class Bank:
        def __init__(self, **kwargs):
            seen["selectors"] = kwargs

        def restore(self, *args, **kwargs):
            seen["restore"] = kwargs

    monkeypatch.setattr(restore_cli, "DockerBank", Bank)
    assert restore_cli.main(["--backup-file", str(_input_dump(tmp_path)), "--yes", "--leave-stopped",
                             "--container", "project-pg", "--daemon-container", "project-daemon", "--db", "bank",
                             "--user", "owner", "--db-port", "6543"]) == 0
    assert seen["selectors"] == {"container": "project-pg", "daemon_container": "project-daemon", "db": "bank", "user": "owner",
                                 "db_port": 6543, "daemon_url": None}
    assert seen["restore"]["leave_stopped"] is True


@pytest.mark.parametrize("corruption", ["footer", "crc"])
def test_corrupt_gzip_state_refuses_before_any_mutation(docker_bank, tmp_path, corruption):
    bank, world = docker_bank
    state = tmp_path / "bad.tgz"
    data = bytearray(_state_bytes())
    if corruption == "footer":
        data = data[:-8]
    else:
        data[-8] ^= 0xFF
    state.write_bytes(data)
    before = len(world.calls)
    with pytest.raises(up.UpdateError, match="archive"):
        bank.restore(_input_dump(tmp_path), state_archive=state)
    assert len(world.calls) == before


# A stored block whose length and complement disagree: zlib raises
# zlib.error ("invalid stored block lengths"), not OSError or EOFError.
CORRUPT_DEFLATE = bytes([0x1F, 0x8B, 8, 0, 0, 0, 0, 0, 0, 0xFF, 0x01, 0x05, 0x00, 0x00, 0x00])


def test_a_corrupt_deflate_state_archive_is_refused_not_a_traceback(tmp_path):
    from pseudolife_memory.docker_backup_restore import validate_state
    state = tmp_path / "bad.tgz"
    state.write_bytes(CORRUPT_DEFLATE)
    with pytest.raises(up.UpdateError, match="state archive is unreadable") as caught:
        validate_state(state)
    assert caught.value.exit_code == 2


@pytest.mark.parametrize("which", ["dump", "state"])
def test_restore_command_refuses_corrupt_deflate_input_cleanly(tmp_path, monkeypatch, capsys, which):
    from pseudolife_memory import restore_cli
    monkeypatch.setattr(up, "run_cli", lambda *a, **kw: pytest.fail("no Docker call for an unreadable input"))
    dump = _input_dump(tmp_path)
    argv = ["--backup-file", str(dump), "--yes"]
    if which == "dump":
        dump.write_bytes(CORRUPT_DEFLATE)
        expected = "restore failed: the backup is unreadable or incomplete; use a complete .sql.gz dump. Nothing was restored"
    else:
        state = tmp_path / "bad.tgz"
        state.write_bytes(CORRUPT_DEFLATE)
        argv += ["--state-archive", str(state)]
        expected = ("restore failed: the state archive is unreadable or contains unsafe paths; use a complete, "
                    "unmodified state backup. Nothing was restored")
    assert restore_cli.main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == expected


@pytest.mark.parametrize("which", ["dump", "state"])
def test_a_corrupt_safety_backup_copy_restarts_the_daemon_and_says_retry(docker_bank, tmp_path, which):
    bank, world = docker_bank
    original = world.run

    def corrupt_copy(argv, **kwargs):
        args = list(map(str, argv[1:]))
        if args[0] == "cp" and ":" in args[1] and not Path(args[1]).is_file():
            is_dump = args[1].startswith(world.pg_name + ":")
            if is_dump == (which == "dump"):
                world.calls.append(args)
                Path(args[2]).write_bytes(CORRUPT_DEFLATE)
                return 0, ""
        return original(argv, **kwargs)

    # The fixture's copy_from_container stand-in calls world.run.
    world.run = corrupt_copy
    with pytest.raises(up.UpdateError, match="retry backup before restoring anything"):
        bank.restore(_input_dump(tmp_path))
    assert not any("DROP DATABASE" in " ".join(c) for c in world.calls)
    assert ["start", up.DAEMON_CONTAINER] in world.calls
    safety = tmp_path / "home" / ".pseudolife-mcp" / "backups"
    assert [p.name for p in safety.iterdir() if p.name.endswith(".part")] == []


@pytest.mark.parametrize("output", ['{"Id": "x"}', "{}", "[" * 100000 + "]" * 100000], ids=["object", "empty-object", "deep"])
def test_unusable_inspect_json_is_a_clean_refusal(docker_bank, monkeypatch, output):
    # A JSON object raises KeyError on [0]; deep nesting raises RecursionError.
    bank, world = docker_bank
    monkeypatch.setattr(up, "run_cli", lambda argv, **kw: (0, output))
    with pytest.raises(up.UpdateError, match="cannot inspect probe; restore its Docker container") as caught:
        bank.inspect("probe")
    assert caught.value.exit_code == 2


def test_symlink_chain_cannot_escape_through_parent_components(docker_bank, tmp_path):
    bank, world = docker_bank
    state = tmp_path / "chain.tgz"
    with tarfile.open(state, "w:gz") as archive:
        for name, target in (("alias", "."), ("config.yaml", "alias/../outside/config.yaml")):
            entry = tarfile.TarInfo(name)
            entry.type = tarfile.SYMTYPE
            entry.linkname = target
            archive.addfile(entry)
    before = len(world.calls)
    with pytest.raises(up.UpdateError, match="unsafe"):
        bank.restore(_input_dump(tmp_path), state_archive=state)
    assert len(world.calls) == before


def test_internal_parent_symlink_remains_valid(tmp_path):
    from pseudolife_memory.docker_backup_restore import validate_state
    state = tmp_path / "internal.tgz"
    with tarfile.open(state, "w:gz") as archive:
        entry = tarfile.TarInfo("dir/link")
        entry.type = tarfile.SYMTYPE
        entry.linkname = "../config.yaml"
        archive.addfile(entry)
    validate_state(state)


def test_recovery_command_retains_all_effective_target_selectors(docker_bank, tmp_path):
    bank, world = docker_bank
    bank.pg, bank.daemon, bank.db, bank.user = "custom-pg", "custom-daemon", "other_bank", "other_owner"
    world.pg_name = bank.pg
    world.fail_replay = True
    with pytest.raises(up.UpdateError) as exc:
        bank.restore(_input_dump(tmp_path), leave_stopped=True)
    command = str(exc.value)
    assert "--container custom-pg" in command and "--daemon-container custom-daemon" in command
    assert "--db other_bank" in command and "--user other_owner" in command and "--leave-stopped" in command


def test_safety_backup_never_rotates_selected_restore_inputs(docker_bank, tmp_path):
    import os
    bank, _ = docker_bank
    directory = tmp_path / "home" / ".pseudolife-mcp" / "backups"
    directory.mkdir(parents=True)
    for index in range(3):
        dump = directory / f"pseudolife_memory-20200101-000000-{index:08x}.sql.gz"
        state = directory / f"pseudolife_state-20200101-000000-{index:08x}.tgz"
        dump.write_bytes(gzip.compress(b"-- PostgreSQL database dump complete\n"))
        state.write_bytes(_state_bytes())
        os.utime(dump, (0, 0))
        os.utime(state, (0, 0))
    selected_dump = directory / "pseudolife_memory-20200101-000000-00000000.sql.gz"
    selected_state = directory / "pseudolife_state-20200101-000000-00000000.tgz"
    bank.restore(selected_dump, state_archive=selected_state, leave_stopped=True)
    assert selected_dump.exists() and selected_state.exists()


def test_missing_destination_database_can_be_recovered(docker_bank, tmp_path):
    bank, world = docker_bank
    world.database_exists = False
    result = bank.restore(_input_dump(tmp_path), leave_stopped=True)
    assert result["safety_dump"] is None and result["safety_state"].exists()
    assert not any("pg_dump" in " ".join(c) for c in world.calls)


def test_restore_requires_daemon_readiness_before_success(docker_bank, tmp_path, monkeypatch):
    bank, world = docker_bank
    monkeypatch.setattr(up, "fetch_json", lambda *a, **kw: None)
    with pytest.raises(up.UpdateError):
        bank.restore(_input_dump(tmp_path))
    assert world.calls.count(["stop", up.DAEMON_CONTAINER]) >= 2


def test_destructive_boundary_is_visible_before_drop(docker_bank, tmp_path, monkeypatch):
    bank, world = docker_bank

    class BufferedOutput:
        pending = ""
        visible = ""

        def write(self, value):
            self.pending += value
            return len(value)

        def flush(self):
            self.visible += self.pending
            self.pending = ""

    output = BufferedOutput()
    monkeypatch.setattr(sys, "stdout", output)
    bank.log = lambda message: print(message)
    original = world.run

    def observe_drop(argv, **kwargs):
        if "DROP DATABASE" in " ".join(argv):
            assert "==> Dropping + recreating" in output.visible
        return original(argv, **kwargs)

    monkeypatch.setattr(up, "run_cli", observe_drop)
    bank.restore(_input_dump(tmp_path), leave_stopped=True)


def test_archive_checks_link_meaning_at_each_extraction_step(tmp_path):
    from pseudolife_memory.docker_backup_restore import validate_state
    state = tmp_path / "changing-links.tgz"
    with tarfile.open(state, "w:gz") as archive:
        for name, target in (("alias", "."), ("config.yaml", "alias/../outside/config.yaml"), ("alias", "safe")):
            entry = tarfile.TarInfo(name)
            entry.type = tarfile.SYMTYPE
            entry.linkname = target
            archive.addfile(entry)
    with pytest.raises(up.UpdateError, match="unsafe"):
        validate_state(state)


def test_database_alias_on_another_network_does_not_select_wrong_bank(docker_bank):
    from pseudolife_memory.docker_backup_restore import DockerBank
    _, world = docker_bank
    world.daemon["NetworkSettings"] = {"Networks": {"other-project": {}}}
    with pytest.raises(up.UpdateError, match="--container"):
        DockerBank()
    with pytest.raises(up.UpdateError):
        DockerBank(container=up.PG_CONTAINER)


def test_recovery_quotes_space_and_apostrophe_arguments():
    import os
    import shlex
    from pseudolife_memory.docker_backup_restore import _shell_command
    argv = ["pseudolife-mcp", "restore", "--backup-file", "folder with space/bank's dump.sql.gz"]
    command = _shell_command(argv)
    if os.name == "nt":
        assert "'folder with space/bank''s dump.sql.gz'" in command
    else:
        assert shlex.split(command) == argv


def test_absent_port_bindings_still_allow_leave_stopped(docker_bank, tmp_path):
    from pseudolife_memory.docker_backup_restore import DockerBank
    _, world = docker_bank
    world.daemon["HostConfig"]["PortBindings"] = None
    world.daemon["NetworkSettings"]["Ports"] = None
    bank = DockerBank()
    assert bank.restore(_input_dump(tmp_path), leave_stopped=True)["left_stopped"] is True


def test_readiness_uses_targets_nondefault_published_port(docker_bank, tmp_path, monkeypatch):
    bank, _ = docker_bank
    seen = []
    monkeypatch.setattr(up, "fetch_json", lambda url, **kwargs: seen.append(url) or {"status": "ok", "ready": True})
    assert bank.restore(_input_dump(tmp_path))["left_stopped"] is False
    assert seen == ["http://127.0.0.1:18765/health"]


def test_safety_directory_follows_links_before_checking_clone_ancestry(docker_bank, tmp_path, monkeypatch):
    bank, world = docker_bank
    original = Path.resolve
    default = tmp_path / "home" / ".pseudolife-mcp" / "backups"
    linked = tmp_path / "clone" / "data" / "backups"
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **kw: linked if self == default else original(self, *a, **kw))
    with pytest.raises(up.UpdateError, match="--safety-dir"):
        bank.restore(_input_dump(tmp_path), leave_stopped=True)
    assert ["stop", up.DAEMON_CONTAINER] not in world.calls
    outside = tmp_path / "outside-safety"
    result = bank.restore(_input_dump(tmp_path), leave_stopped=True, safety_dir=outside)
    assert result["safety_dump"].is_relative_to(outside)


def test_safety_backup_cannot_land_inside_state_mount_being_replaced(docker_bank, tmp_path):
    bank, world = docker_bank
    bank.running["Mounts"] = [{"Destination": "/data", "Type": "bind", "Source": str(tmp_path / "home" / ".pseudolife-mcp")}]
    with pytest.raises(up.UpdateError, match="--safety-dir"):
        bank.restore(_input_dump(tmp_path), leave_stopped=True)
    assert ["stop", up.DAEMON_CONTAINER] not in world.calls
    external = tmp_path / "external-safety"
    result = bank.restore(_input_dump(tmp_path), leave_stopped=True, safety_dir=external)
    assert result["safety_dump"].is_relative_to(external)


@pytest.mark.parametrize("url,extra,expected", [
    ("postgresql://owner:fixture@db", [], ("owner", "owner")),
    ("postgresql://owner:fixture@db", ["PGDATABASE=connection_bank"], ("owner", "connection_bank")),
    ("postgresql://db", ["PGUSER=connection_owner"], ("connection_owner", "connection_owner")),
    ("postgresql://owner:fixture@db/explicit_bank", ["PGDATABASE=other"], ("owner", "explicit_bank")),
])
def test_database_defaults_follow_daemon_libpq_not_postgres_initialization(docker_bank, url, extra, expected):
    from pseudolife_memory.docker_backup_restore import DockerBank
    _, world = docker_bank
    world.daemon["Config"]["Env"] = ["PSEUDOLIFE_MCP_DATABASE_URL=" + url, *extra]
    bank = DockerBank()
    assert (bank.user, bank.db) == expected


def test_ambiguous_daemon_database_requires_explicit_selectors(docker_bank):
    from pseudolife_memory.docker_backup_restore import DockerBank
    _, world = docker_bank
    world.daemon["Config"]["Env"] = []
    with pytest.raises(up.UpdateError, match="--db.*--user"):
        DockerBank()
    bank = DockerBank(container=up.PG_CONTAINER, db="actual_bank", user="actual_owner", db_port=5432)
    assert (bank.user, bank.db) == ("actual_owner", "actual_bank")


def test_restore_waits_past_ok_but_not_ready(docker_bank, tmp_path, monkeypatch):
    bank, _ = docker_bank
    replies = iter([{"status": "ok", "ready": False, "pending": ["init"]}, {"status": "ok", "ready": True}])
    sleeps = []
    monkeypatch.setattr(up, "fetch_json", lambda *a, **kw: next(replies))
    monkeypatch.setattr(up, "sleep", lambda seconds: sleeps.append(seconds))
    bank.restore(_input_dump(tmp_path))
    assert sleeps == [1.5]


@pytest.mark.parametrize("version,succeeds", [("0.19.0", True), ("0.20.0", False)])
def test_readiness_compatibility_is_limited_to_pre_contract_releases(docker_bank, tmp_path, monkeypatch, version, succeeds):
    bank, _ = docker_bank
    monkeypatch.setattr(up, "fetch_json", lambda *a, **kw: {"status": "ok", "version": version})
    if succeeds:
        bank.restore(_input_dump(tmp_path))
    else:
        with pytest.raises(up.UpdateError):
            bank.restore(_input_dump(tmp_path))


def test_opaque_libpq_service_requires_explicit_target(docker_bank):
    from pseudolife_memory.docker_backup_restore import DockerBank
    _, world = docker_bank
    world.daemon["Config"]["Env"] = ["PSEUDOLIFE_MCP_DATABASE_URL=service=fixture host=db user=owner", "PGDATABASE=not_the_service_bank"]
    with pytest.raises(up.UpdateError, match="--container.*--db.*--user"):
        DockerBank()
    bank = DockerBank(container=up.PG_CONTAINER, db="service_bank", user="service_owner", db_port=5432)
    assert (bank.user, bank.db) == ("service_owner", "service_bank")


@pytest.mark.skipif(sys.platform != "win32", reason="Docker Desktop bind-path mapping is Windows-only")
@pytest.mark.parametrize("prefix", ["/host_mnt", "/run/desktop/mnt/host"])
def test_desktop_mount_mapping_keeps_safety_outside_state(docker_bank, tmp_path, prefix):
    bank, _ = docker_bank
    source = tmp_path / "home" / ".pseudolife-mcp"
    mapped = prefix + "/" + source.drive[0].lower() + "/" + source.as_posix()[3:]
    bank.running["Mounts"] = [{"Destination": "/data", "Type": "bind", "Source": mapped}]
    with pytest.raises(up.UpdateError, match="--safety-dir"):
        bank.restore(_input_dump(tmp_path), leave_stopped=True)


@pytest.mark.parametrize("env,changes,expected", [
    (["PSEUDOLIFE_MCP_DATABASE_URL=postgresql://", "PGHOST=db", "PGUSER=runtime", "PGPORT=6543"], {}, ("runtime", "runtime", 6543)),
    (["PSEUDOLIFE_MCP_DATABASE_URL=host=db"], {"User": "worker"}, ("worker", "worker", 5432)),
    (["PSEUDOLIFE_MCP_DATABASE_URL=host=db"], {"User": ""}, ("root", "root", 5432)),
])
def test_docker_bank_uses_the_same_runtime_defaults(docker_bank, env, changes, expected):
    from pseudolife_memory.docker_backup_restore import DockerBank
    _, world = docker_bank
    world.daemon["Config"].update(changes)
    world.daemon["Config"]["Env"] = env
    bank = DockerBank()
    assert (bank.user, bank.db, bank.db_port) == expected


@pytest.mark.parametrize("address,shared,accept", [("10.0.0.2", True, True), ("10.0.0.3", True, False), ("10.0.0.2", False, False)])
def test_docker_bank_hostaddr_requires_selected_ip_on_shared_network(docker_bank, address, shared, accept):
    from pseudolife_memory.docker_backup_restore import DockerBank
    _, world = docker_bank
    world.daemon["Config"]["Env"].append("PGHOSTADDR=" + address)
    world.pg["NetworkSettings"]["Networks"] = {"fixture" if shared else "other": {"IPAddress": "10.0.0.2", "Aliases": ["db"]}}
    if accept:
        assert DockerBank().db == "custom_bank"
    else:
        with pytest.raises(up.UpdateError):
            DockerBank(container=up.PG_CONTAINER)


def test_explicit_opaque_target_also_needs_its_port(docker_bank):
    from pseudolife_memory.docker_backup_restore import DockerBank
    _, world = docker_bank
    world.daemon["Config"]["Env"] = ["PSEUDOLIFE_MCP_DATABASE_URL=service=opaque"]
    with pytest.raises(up.UpdateError, match="--db-port"):
        DockerBank(container=up.PG_CONTAINER, db="bank", user="owner")
    bank = DockerBank(container=up.PG_CONTAINER, db="bank", user="owner", db_port=6543)
    assert (bank.user, bank.db, bank.db_port) == ("owner", "bank", 6543)


@pytest.mark.parametrize("dsn", [None, ""])
def test_file_mode_daemon_requires_explicit_standalone_target(docker_bank, tmp_path, dsn):
    from pseudolife_memory.docker_backup_restore import DockerBank
    _, world = docker_bank
    world.daemon["Config"]["Env"] = ["PGHOST=db", "PGUSER=ambient_role", "PGDATABASE=ambient_bank"]
    world.daemon["Config"]["User"] = "misleading_role"
    if dsn is not None:
        world.daemon["Config"]["Env"].append("PSEUDOLIFE_MCP_DATABASE_URL=" + dsn)
    before = len(world.calls)
    with pytest.raises(up.UpdateError, match="PSEUDOLIFE_MCP_DATABASE_URL"):
        DockerBank()
    assert all(call[0] == "inspect" for call in world.calls[before:])
    assert not (tmp_path / "artifacts").exists()
    bank = DockerBank(container=up.PG_CONTAINER, db="chosen_bank", user="chosen_role", db_port=6543)
    assert (bank.user, bank.db, bank.db_port) == ("chosen_role", "chosen_bank", 6543)


def test_restore_streams_the_dump_into_postgres_as_the_container_user(docker_bank, tmp_path):
    # docker cp creates root-owned files: the non-root PostgreSQL container
    # (uid 999) could not delete one from sticky /tmp, nor read a private
    # (0600) host backup, so the dump arrives through docker exec -i instead.
    bank, world = docker_bank
    dump = _input_dump(tmp_path)
    bank.restore(dump, safety_dir=tmp_path / "safety")
    pg = world.pg_name + ":"
    assert not [c for c in world.calls if c[0] == "cp" and c[-1].startswith(pg)]
    streamed = [c for c in world.calls if c[:3] == ["exec", "-i", world.pg_name] and "<" in c]
    assert len(streamed) == 1 and streamed[0][-1] == str(dump)
    remote = streamed[0][-3]
    assert ["exec", world.pg_name, "rm", "-f", remote, remote + ".sql"] in world.calls


def test_copy_into_container_streams_stdin_through_docker_exec(tmp_path, monkeypatch):
    from pseudolife_memory import docker_backup_restore as module
    source = tmp_path / "private.sql.gz"
    source.write_bytes(b"payload")
    seen = []

    def fake_run(argv, **kwargs):
        seen.append((argv, kwargs["stdin"].read()))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module.copy_into_container("docker", source, "pg", "/tmp/x.sql.gz")
    assert seen == [(["docker", "exec", "-i", "pg", "sh", "-c", 'umask 077 && cat > "$1"', "_", "/tmp/x.sql.gz"],
                     b"payload")]
    monkeypatch.setattr(module.subprocess, "run", lambda argv, **kwargs: types.SimpleNamespace(returncode=1))
    with pytest.raises(up.UpdateError, match="could not be copied into pg"):
        module.copy_into_container("docker", source, "pg", "/tmp/x.sql.gz")
