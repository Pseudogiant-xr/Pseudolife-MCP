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


@pytest.mark.parametrize("dsn,env,expected", [
    ("host=external-db hostaddr='' user=owner dbname=bank", {"PGHOSTADDR": "10.0.0.2"}, None),
    ("host='' user=owner dbname=bank", {"PGHOST": "pseudolife-pg"}, None),
    ("host=pseudolife-pg user='' dbname=bank", {"PGUSER": "ambient"}, ("runtime_user", "bank", 5432)),
    ("host=pseudolife-pg user=owner dbname=''", {"PGDATABASE": "ambient"}, ("owner", "owner", 5432)),
    ("host=pseudolife-pg user=owner dbname=bank port=''", {"PGPORT": "6543"}, ("owner", "bank", 5432)),
    ("host=pseudolife-pg user=owner dbname=bank service=''", {"PGSERVICE": "opaque"}, ("owner", "bank", 5432)),
])
def test_present_empty_dsn_fields_override_the_daemon_environment(dsn, env, expected):
    config = containers(dsn, **env)
    daemon, postgres = config[up.DAEMON_CONTAINER], config[up.PG_CONTAINER]
    daemon["Config"]["User"] = "runtime_user"
    postgres["NetworkSettings"]["Networks"]["bank"]["IPAddress"] = "10.0.0.2"
    if expected is None:
        with pytest.raises((ValueError, up.UpdateError)):
            up._backup_connection_details(daemon, postgres)
    else:
        assert up._backup_connection_details(daemon, postgres) == expected


def fake_backup(monkeypatch, tmp_path, config, *, dump_rc=0, tar_rc=0):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "df" in argv:
            return 0, "overlay 99999999 1 99999998 1% /\n"
        if argv[1] == "inspect":
            if "{{.State.Running}}" in argv:
                return 0, str(config[argv[-1]].get("State", {}).get("Running", True)).lower()
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
    ("postgresql://custom@pseudolife-pg:5432/bank", {"PGPORT": "9999"}, ("custom", "bank")),
    ("postgresql://custom@pseudolife-pg/bank?port=5432", {"PGPORT": "9999"}, ("custom", "bank")),
    ("user=custom dbname=bank", {"PGHOST": "pseudolife-pg"}, ("custom", "bank")),
    ("postgresql://custom@/bank", {"PGHOST": "pseudolife-pg"}, ("custom", "bank")),
    ("postgresql://custom@pseudolife-pg/bank?sslmode=verify-full&sslrootcert=/fixture/root.pem", {}, ("custom", "bank")),
    ("host=pseudolife-pg user=custom dbname=bank sslmode=verify-full sslrootcert=/fixture/root.pem", {}, ("custom", "bank")),
])
def test_builtin_backup_uses_daemon_connection(monkeypatch, tmp_path, dsn, env, expected):
    calls = fake_backup(monkeypatch, tmp_path, containers(dsn, **env))
    up.Update(up.Options()).builtin_backup()
    dump = next(c for c in calls if any("pg_dump" in a for a in c))
    from psycopg.conninfo import conninfo_to_dict
    assert dump[-2] == expected[0]
    assert conninfo_to_dict(dump[-1]) == {"dbname": expected[1]}
    assert all("fixture" not in a for a in dump)


@pytest.mark.parametrize("dsn,env", [
    ("", {}), ("service=opaque", {}), ("invalid fixture", {}),
    ("host=pseudolife-pg hostaddr=10.0.0.2 user=custom dbname=bank", {}),
    ("postgresql://custom@pseudolife-pg/bank?hostaddr=10.0.0.2", {}),
    ("postgresql://custom@elsewhere/bank", {}),
    ("postgresql://custom@pseudolife-pg/bank", {"PGSERVICE": "opaque"}),
    ("postgresql://custom@pseudolife-pg/bank", {"PGHOSTADDR": "10.0.0.1"}),
    ("postgresql://pseudolife-pg/bank", {}),
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


@pytest.mark.parametrize("dsn,env,expected", [
    ("postgresql://custom@pseudolife-pg:6543/bank", {"PGPORT": "7654"}, ("custom", "bank", 6543)),
    ("postgresql://custom@pseudolife-pg/bank", {"PGPORT": "6543"}, ("custom", "bank", 6543)),
    ("host=pseudolife-pg user=custom dbname='host=elsewhere'", {}, ("custom", "host=elsewhere", 5432)),
    ("postgresql://custom@pseudolife-pg/postgresql%3A%2F%2Felsewhere", {}, ("custom", "postgresql://elsewhere", 5432)),
    ("host=pseudolife-pg user=custom dbname='bank with spaces'", {}, ("custom", "bank with spaces", 5432)),
    ("host=pseudolife-pg user=custom dbname='bank\\'quote'", {}, ("custom", "bank'quote", 5432)),
    ("postgresql://", {"PGHOST": "pseudolife-pg", "PGUSER": "runtime"}, ("runtime", "runtime", 5432)),
    ("host=pseudolife-pg user=custom hostaddr=10.0.0.2", {}, ("custom", "custom", 5432)),
    ("host=pseudolife-pg user=custom", {"PGHOSTADDR": "10.0.0.2"}, ("custom", "custom", 5432)),
])
def test_builtin_backup_preserves_effective_runtime_target(monkeypatch, tmp_path, dsn, env, expected):
    from psycopg.conninfo import conninfo_to_dict
    config = containers(dsn, **env)
    config[up.PG_CONTAINER]["Config"]["Env"].append("PGPORT=9999")
    config[up.PG_CONTAINER]["NetworkSettings"]["Networks"]["bank"]["IPAddress"] = "10.0.0.2"
    calls = fake_backup(monkeypatch, tmp_path, config)
    up.Update(up.Options()).builtin_backup()
    dump = next(c for c in calls if any("pg_dump" in a for a in c))
    assert dump[-3:-1] == [str(expected[2]), expected[0]]
    assert conninfo_to_dict(dump[-1]) == {"dbname": expected[1]}
    script = dump[dump.index("-c") + 1]
    assert 'unset PGHOSTADDR PGSERVICE PGSERVICEFILE;' in script
    assert '-h /var/run/postgresql' in script


@pytest.mark.parametrize("runtime_user,expected", [("worker", "worker"), ("", "root")])
def test_backup_default_role_is_the_daemon_os_user(runtime_user, expected):
    config = containers("host=pseudolife-pg")
    config[up.DAEMON_CONTAINER]["Config"]["User"] = runtime_user
    assert up._backup_connection_details(config[up.DAEMON_CONTAINER], config[up.PG_CONTAINER]) == (expected, expected, 5432)


@pytest.mark.parametrize("dsn,env", [
    ("host=pseudolife-pg,elsewhere user=custom", {}),
    ("host=pseudolife-pg user=custom port=5432,6543", {}),
    ("host=pseudolife-pg user=custom", {"PGHOSTADDR": "10.0.0.3"}),
    ("host=pseudolife-pg user=custom service=opaque", {}),
])
def test_richer_backup_target_refuses_ambiguous_authority(monkeypatch, tmp_path, dsn, env):
    config = containers(dsn, **env)
    config[up.PG_CONTAINER]["NetworkSettings"]["Networks"]["bank"]["IPAddress"] = "10.0.0.2"
    calls = fake_backup(monkeypatch, tmp_path, config)
    with pytest.raises(up.UpdateError):
        up.Update(up.Options()).builtin_backup()
    assert all(c[1] == "inspect" for c in calls)
    assert not (tmp_path / "backups").exists()


def test_legacy_backup_pair_wrapper_does_not_admit_a_nondefault_port():
    config = containers("postgresql://custom@pseudolife-pg:6543/bank")
    with pytest.raises(ValueError):
        up._backup_connection_parts(config[up.DAEMON_CONTAINER], config[up.PG_CONTAINER])


def test_numeric_daemon_uid_does_not_guess_the_runtime_database_role():
    config = containers("host=pseudolife-pg")
    config[up.DAEMON_CONTAINER]["Config"]["User"] = "1000:1000"
    with pytest.raises(ValueError):
        up._backup_connection_details(config[up.DAEMON_CONTAINER], config[up.PG_CONTAINER])


def test_connection_parse_error_does_not_disclose_the_source():
    config = containers("postgresql://custom:private-fixture@db/bank?unknown=private-fixture")
    with pytest.raises(ValueError) as error:
        up._backup_connection_details(config[up.DAEMON_CONTAINER], config[up.PG_CONTAINER])
    assert "private-fixture" not in str(error.value)


@pytest.mark.parametrize("dsn", [None, ""])
def test_file_mode_daemon_never_infers_a_database_from_ambient_libpq(monkeypatch, tmp_path, dsn):
    config = containers(dsn or "", PGHOST="pseudolife-pg", PGUSER="ambient_role", PGDATABASE="ambient_bank")
    daemon = config[up.DAEMON_CONTAINER]
    daemon["Config"]["User"] = "misleading_role"
    if dsn is None:
        daemon["Config"]["Env"] = [item for item in daemon["Config"]["Env"]
                                    if not item.startswith("PSEUDOLIFE_MCP_DATABASE_URL=")]
    calls = fake_backup(monkeypatch, tmp_path, config)
    with pytest.raises(up.UpdateError, match="PSEUDOLIFE_MCP_DATABASE_URL"):
        up.Update(up.Options()).builtin_backup()
    assert all(c[1] == "inspect" for c in calls)
    assert not (tmp_path / "backups").exists()
