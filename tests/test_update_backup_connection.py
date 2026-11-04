"""Built-in update backups follow the daemon, never PostgreSQL init settings."""
import gzip
import json

import pytest

from pseudolife_memory import update_cli as up


def containers(dsn="postgresql://custom:fixture@pseudolife-pg/custom_bank", **env):
    network = {"bank": {"Aliases": ["pseudolife-pg"]}}
    return {
        up.DAEMON_CONTAINER: {"Config": {"Env": [f"{k}={v}" for k, v in
            {"PSEUDOLIFE_MCP_DATABASE_URL": dsn, **env}.items()]},
            "NetworkSettings": {"Networks": {"bank": {}}}},
        up.PG_CONTAINER: {"Name": "/" + up.PG_CONTAINER,
            "Config": {"Env": ["POSTGRES_USER=wrong", "POSTGRES_DB=wrong"]},
            "NetworkSettings": {"Networks": network}},
    }


def fake_backup(monkeypatch, tmp_path, config, *, dump_rc=0, tar_rc=0):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "inspect":
            return 0, json.dumps([config[argv[-1]]])
        if argv[1] == "cp":
            from pathlib import Path
            Path(argv[-1]).write_bytes(gzip.compress(b"-- PostgreSQL database dump complete\n"))
        if any("pg_dump" in a for a in argv):
            return dump_rc, "fixture diagnostic"
        if any("tar czf" in a for a in argv):
            return tar_rc, "stopped"
        return 0, ""

    monkeypatch.setattr(up, "run_cli", run)
    monkeypatch.setattr(up, "data_dir", lambda: tmp_path)
    return calls


@pytest.mark.parametrize("dsn,env,expected", [
    ("postgresql://custom:fixture@pseudolife-pg/custom_bank", {}, ("custom", "custom_bank")),
    ("postgresql://pseudolife:fixture@pseudolife-pg/pseudolife_memory", {}, ("pseudolife", "pseudolife_memory")),
    ("postgresql://custom:fixture@pseudolife-pg", {}, ("custom", "custom")),
    ("postgresql://custom:fixture@pseudolife-pg", {"PGDATABASE": "runtime_bank"}, ("custom", "runtime_bank")),
    ("host=pseudolife-pg user=custom dbname='bank with spaces' password=fixture", {}, ("custom", "bank with spaces")),
    ("postgresql://pseudolife-pg", {"PGUSER": "runtime_user"}, ("runtime_user", "runtime_user")),
    ("postgresql://custom:fixture@pseudolife-pg/wrong?dbname=actual", {}, ("custom", "actual")),
])
def test_builtin_backup_uses_daemon_connection(monkeypatch, tmp_path, dsn, env, expected):
    calls = fake_backup(monkeypatch, tmp_path, containers(dsn, **env))
    up.Update(up.Options()).builtin_backup()
    dump = next(c for c in calls if any("pg_dump" in a for a in c))
    assert dump[-2:] == list(expected)
    assert all("fixture" not in a for a in dump)


@pytest.mark.parametrize("dsn,env", [
    ("", {}), ("service=opaque", {}), ("invalid fixture", {}),
    ("host=pseudolife-pg hostaddr=10.0.0.2 user=custom dbname=bank", {}),
    ("postgresql://custom@pseudolife-pg/bank?hostaddr=10.0.0.2", {}),
    ("postgresql://custom@elsewhere/bank", {}),
    ("postgresql://custom@pseudolife-pg/bank", {"PGSERVICE": "opaque"}),
    ("postgresql://custom@pseudolife-pg/bank", {"PGHOSTADDR": "10.0.0.1"}),
    ("postgresql://pseudolife-pg/bank", {}),
    ("host=pseudolife-pg user=custom dbname='host=elsewhere'", {}),
    ("postgresql://custom@pseudolife-pg:9999/bank", {}),
])
def test_opaque_backup_connection_refuses_before_writing(monkeypatch, tmp_path, dsn, env):
    calls = fake_backup(monkeypatch, tmp_path, containers(dsn, **env))
    with pytest.raises(up.UpdateError, match="PSEUDOLIFE_MCP_DATABASE_URL|PGSERVICE|PGHOSTADDR"):
        up.Update(up.Options()).builtin_backup()
    assert all(c[1] == "inspect" for c in calls)
    assert not (tmp_path / "backups").exists()


def test_stopped_daemon_state_archive_still_retains_dump(monkeypatch, tmp_path):
    config = containers()
    config[up.DAEMON_CONTAINER]["State"] = {"Running": False}
    fake_backup(monkeypatch, tmp_path, config, tar_rc=1)
    result = up.Update(up.Options()).builtin_backup()
    assert result["dump"] and result["state"] is None


def test_legacy_state_mount_name_still_archives_data(monkeypatch, tmp_path):
    config = containers()
    config[up.DAEMON_CONTAINER]["Mounts"] = [{"Destination": "/data", "Name": "legacy_state"}]
    calls = fake_backup(monkeypatch, tmp_path, config)
    up.Update(up.Options()).builtin_backup()
    archive = next(c for c in calls if any("tar czf" in a for a in c))
    assert archive[-1].endswith("-C /data .")


def test_dump_failure_does_not_echo_connection_diagnostics(monkeypatch, tmp_path, capsys):
    fake_backup(monkeypatch, tmp_path, containers(), dump_rc=1)
    with pytest.raises(up.UpdateError) as error:
        up.Update(up.Options()).builtin_backup()
    assert "fixture diagnostic" not in str(error.value)
    assert "fixture" not in capsys.readouterr().out
