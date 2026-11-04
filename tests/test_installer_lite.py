"""Docker preflight preserves existing lite banks, including Windows defaults."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
BEGIN = "# >>> pseudolife-mcp install (managed block — installer rewrites between markers) >>>"
END = "# <<< pseudolife-mcp install <<<"


@pytest.fixture
def guard(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("lite_preflight", ROOT / "ops/preflight.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    containers, volumes, busy, calls = [], [], set(), []

    def docker_run(argv, **kwargs):
        calls.append(argv[1:])
        if argv[1:3] == ["ps", "-a"]:
            out = "\n".join(c["Id"] for c in containers)
        elif argv[1] == "inspect":
            out = json.dumps(containers)
        elif argv[1:3] == ["volume", "ls"]:
            out = "\n".join(volumes)
        elif argv[1] == "compose":
            source = Path(argv[argv.index("--env-file") + 1])
            out = "\n".join(f"{key}={value}" for key, value in module.environment(source)[0].items())
        else:
            pytest.fail(f"Unexpected Docker operation: {argv[1:]}")
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(module.subprocess, "run", docker_run)
    monkeypatch.setattr(module, "port_busy", lambda port: port in busy)
    env = tmp_path / "ops/.env"
    env.parent.mkdir()
    return module, env, containers, volumes, busy, calls


def container(*, running=True):
    return {"Id": "fixture-daemon", "Name": "/pseudolife-mcp-daemon",
            "Config": {"Labels": {"com.docker.compose.project": "pseudolife-mcp"}},
            "State": {"Running": running}, "Mounts": [],
            "NetworkSettings": {"Ports": {"8765/tcp": [{"HostPort": "8765"}]}}}


@pytest.fixture(autouse=True)
def disposable_home(monkeypatch, tmp_path, guard):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData/Local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local/share"))
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATA_DIR", raising=False)
    return home


def initialized_bank(data_dir):
    pgdata = data_dir / "embedded_pg"
    pgdata.mkdir(parents=True)
    (pgdata / "PG_VERSION").write_text("18\n", encoding="utf-8")
    (pgdata / "postmaster.pid").write_text("999999\n", encoding="utf-8")
    (data_dir / "config.yaml").write_text("# fixture bank\n", encoding="utf-8")
    return data_dir


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def test_initialized_lite_bank_refuses_before_docker_takes_the_free_port(guard, disposable_home):
    module, env, _, _, _, calls = guard
    bank = initialized_bank(disposable_home / ".pseudolife-mcp")
    env.write_text("# keep this environment\n", encoding="utf-8")
    before = snapshot(disposable_home), env.read_bytes()
    with pytest.raises(module.Refusal, match="lite") as error:
        module.check(env)
    message = str(error.value)
    assert "pseudolife-mcp backup --data-dir" in message
    assert str(bank) in message
    assert "restore" in message and "configuration.md#backups" in message
    assert "Nothing changed" in message
    assert (snapshot(disposable_home), env.read_bytes()) == before
    assert all(call[0] in ("ps", "inspect", "compose", "volume") for call in calls)


def test_active_lite_bank_gets_migration_advice_before_generic_busy_port(guard, disposable_home):
    module, env, _, _, busy, _ = guard
    initialized_bank(disposable_home / ".pseudolife-mcp")
    busy.add(8765)
    with pytest.raises(module.Refusal, match="pseudolife-mcp backup --data-dir"):
        module.check(env)


@pytest.mark.parametrize("contents", ["empty", "cache", "stale-pid", "marker-directory"])
def test_empty_or_cache_only_lite_directory_is_not_a_bank(guard, disposable_home, contents):
    module, env, _, _, _, _ = guard
    cache = module.preflight_lite.data_dirs()[0] / "embedded_pg/bin"
    cache.mkdir(parents=True)
    if contents == "cache":
        (cache / "postgres.exe").write_bytes(b"fixture binary cache")
    elif contents == "stale-pid":
        (cache.parent / "postmaster.pid").write_text("999999\n", encoding="utf-8")
    elif contents == "marker-directory":
        (cache.parent / "PG_VERSION").mkdir()
    module.check(env)
    assert not env.exists()


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
@pytest.mark.parametrize("override", [True, False])
def test_platform_default_matches_embedded_runtime_and_is_guarded(guard, monkeypatch, platform, override):
    from pseudolife_memory.storage import embedded_pg

    module, env, _, _, _, _ = guard
    monkeypatch.setattr(module.preflight_lite, "_PLATFORM", platform)
    monkeypatch.setattr(embedded_pg, "_PLATFORM", platform)
    if not override:
        monkeypatch.delenv("LOCALAPPDATA")
        monkeypatch.delenv("XDG_DATA_HOME")
    bank = embedded_pg.default_lite_data_dir()
    assert module.preflight_lite.data_dirs()[0] == bank
    initialized_bank(bank)
    with pytest.raises(module.Refusal, match="lite") as error:
        module.check(env)
    assert str(bank) in str(error.value)
    assert not env.exists()


@pytest.mark.parametrize("relative", [True, False])
def test_custom_data_dir_is_guarded_without_mutating_it(guard, monkeypatch, tmp_path, relative):
    module, env, _, _, _, _ = guard
    bank = initialized_bank(tmp_path / "custom bank")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", "custom bank" if relative else str(bank))
    before = snapshot(bank)
    with pytest.raises(module.Refusal, match="backup --data-dir") as error:
        module.check(env)
    assert '"custom bank"' in str(error.value) if relative else f'"{bank}"' in str(error.value)
    assert snapshot(bank) == before
    assert not env.exists()


def test_custom_empty_directory_does_not_hide_default_bank(guard, monkeypatch, tmp_path):
    module, env, _, _, _, _ = guard
    bank = initialized_bank(module.preflight_lite.data_dirs()[0])
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path / "empty-custom"))
    with pytest.raises(module.Refusal, match="lite") as error:
        module.check(env)
    assert str(bank) in str(error.value)


@pytest.mark.parametrize("running", [True, False])
@pytest.mark.parametrize("host_ip", ["", "127.0.0.1", "0.0.0.0"])
def test_own_configured_docker_reruns_with_stopped_lite_files(guard, running, host_ip):
    module, env, containers, volumes, busy, _ = guard
    bank = initialized_bank(module.preflight_lite.data_dirs()[0])
    before = snapshot(bank)
    env.write_text(f"{BEGIN}\n{END}\n", encoding="utf-8")
    volumes.extend(["pseudolife-mcp-bank", "pseudolife-mcp-state"])
    daemon = container(running=running)
    daemon["NetworkSettings"]["Ports"]["8765/tcp"][0]["HostIp"] = host_ip
    containers.append(daemon)
    if running:
        busy.add(8765)
    module.check(env)
    assert snapshot(bank) == before


def test_own_stopped_docker_does_not_excuse_active_lite_port(guard):
    module, env, containers, _, busy, _ = guard
    bank = initialized_bank(module.preflight_lite.data_dirs()[0])
    before = snapshot(bank)
    env.write_text(f"{BEGIN}\n{END}\n", encoding="utf-8")
    containers.append(container(running=False))
    busy.add(8765)
    with pytest.raises(module.Refusal, match="backup --data-dir"):
        module.check(env)
    assert snapshot(bank) == before


@pytest.mark.parametrize("host_ip", ["::1", "192.0.2.2"])
def test_docker_publication_on_another_address_does_not_excuse_active_lite(guard, host_ip):
    module, env, containers, _, busy, _ = guard
    initialized_bank(module.preflight_lite.data_dirs()[0])
    env.write_text(f"{BEGIN}\n{END}\n", encoding="utf-8")
    daemon = container()
    daemon["NetworkSettings"]["Ports"]["8765/tcp"][0]["HostIp"] = host_ip
    containers.append(daemon)
    busy.add(8765)
    with pytest.raises(module.Refusal, match="backup --data-dir"):
        module.check(env)


@pytest.mark.parametrize("active_lite", [True, False])
def test_own_recorded_volumes_allow_rerun_after_compose_down_unless_lite_port_is_busy(guard, active_lite):
    module, env, _, volumes, busy, _ = guard
    initialized_bank(module.preflight_lite.data_dirs()[0])
    env.write_text(f"{BEGIN}\n{END}\n", encoding="utf-8")
    volumes.extend(["pseudolife-mcp-bank", "pseudolife-mcp-state"])
    if active_lite:
        busy.add(8765)
        with pytest.raises(module.Refusal, match="backup --data-dir"):
            module.check(env)
    else:
        module.check(env)


@pytest.mark.parametrize("missing", ["env", "daemon"])
def test_partial_docker_setup_is_not_a_completed_migration(guard, missing):
    module, env, containers, _, _, _ = guard
    initialized_bank(module.preflight_lite.data_dirs()[0])
    if missing != "env":
        env.write_text(f"{BEGIN}\n{END}\n", encoding="utf-8")
    if missing != "daemon":
        containers.append(container(running=False))
    with pytest.raises(module.Refusal, match="lite"):
        module.check(env)


def test_unrelated_busy_port_without_a_bank_keeps_generic_fix(guard):
    module, env, _, _, busy, _ = guard
    busy.add(8765)
    with pytest.raises(module.Refusal, match="Stop or reconfigure the service listening on 8765"):
        module.check(env)


def test_cli_refuses_with_migration_advice_without_traceback(guard, monkeypatch, capsys):
    module, env, _, _, _, _ = guard
    initialized_bank(module.preflight_lite.data_dirs()[0])
    monkeypatch.setattr(sys, "argv", ["preflight.py", "--env-file", str(env)])
    assert module.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith("FAIL An initialized lite bank")
    assert "backup --data-dir" in output.err
    assert "Traceback" not in output.err
    assert not env.exists()
