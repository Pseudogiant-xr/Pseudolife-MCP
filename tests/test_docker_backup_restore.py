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
    from pseudolife_memory import docker_backup_restore as module

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
