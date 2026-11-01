"""Opt-in engine selection: refusal, persistence, activation and rollback."""
import json
import gzip
import io
import tarfile
import subprocess
from pathlib import Path

import pytest

from pseudolife_memory import update_cli as up


def test_engine_flags_reach_options():
    options = up.options_from_args(up.build_parser().parse_args(["--engine", "rust", "--shadow"]))
    assert options.engine == "rust" and options.shadow


def test_release_rust_refuses_before_docker(monkeypatch, capsys):
    monkeypatch.setattr(up, "run_cli", lambda *a, **kw: pytest.fail("refusal must precede Docker"))
    assert up.main(["--engine", "rust"]) == 2
    assert "--checkout" in capsys.readouterr().err


def test_shadow_requires_explicit_rust(capsys):
    assert up.main(["--shadow"]) == 2
    assert "--engine rust" in capsys.readouterr().err


def test_d2_preflight_rejects_old_schema_and_pending_import():
    from pseudolife_memory.update_engine import require_imported_bank
    for schema, pending in ((56, False), (58, True), (None, False)):
        with pytest.raises(up.UpdateError, match="Python"):
            require_imported_bank(schema, pending)
    require_imported_bank(58, False)


def test_overlay_preserves_persistent_data_and_separates_shadow():
    import yaml
    root = Path(__file__).resolve().parents[1]
    active = yaml.safe_load((root / "ops/docker-compose.rust-daemon.yml").read_text())
    daemon = active["services"][up.DAEMON_SERVICE]
    assert daemon["build"]["dockerfile"] == "ops/Dockerfile.daemon-rust"
    assert "tmpfs" not in daemon
    shadow = yaml.safe_load((root / "ops/docker-compose.rust-shadow.yml").read_text())
    assert up.DAEMON_SERVICE not in shadow["services"]
    service = shadow["services"]["pseudolife-rust-shadow"]
    assert service["environment"]["PSEUDOLIFE_READ_ONLY"] == "1"
    assert "pseudolife_shadow:" in service["environment"]["PSEUDOLIFE_MCP_DATABASE_URL"]
    assert service["ports"] == ["127.0.0.1:${PSEUDOLIFE_SHADOW_PORT:-8766}:8765"]


@pytest.fixture
def engine_host(tmp_path, monkeypatch):
    from pseudolife_memory import update_engine as eng
    repo = tmp_path / "checkout"
    ops = repo / "ops"
    ops.mkdir(parents=True)
    (repo / "pseudolife_memory/storage").mkdir(parents=True)
    (repo / "rust/daemon/src").mkdir(parents=True)
    (repo / "rust/daemon/src/config.rs").write_text("// no read-only support yet\n")
    (repo / "pyproject.toml").write_text('[project]\nversion = "0.21.0"\n')
    (repo / "pseudolife_memory/storage/schema.py").write_text("SCHEMA_META_VERSION = 58\n")
    for name in ("docker-compose.yml", "docker-compose.rust-daemon.yml", "docker-compose.rust-shadow.yml", "backup.sh", "backup.ps1"):
        (ops / name).write_text("# fixture\n")
    (ops / ".env").write_text("# isolated host\n")
    state = {"calls": [], "schema": "58", "pending": False, "read": True, "fail": None,
             "health": {"status": "ok", "version": "0.21.0", "schema": 58},
             "running": {"Image": "sha256:validated-python", "State": {"Running": True},
                         "Mounts": [{"Destination": "/data", "Name": "fixture-state"}],
                         "Config": {"Image": "python:old", "Labels": {}}}}
    config = {"services": {"pseudolife-pg": {"environment": {"POSTGRES_USER": "fixture", "POSTGRES_DB": "fixture"}},
                            up.DAEMON_SERVICE: {"environment": {"PSEUDOLIFE_MCP_TOKEN": "fixture-bearer"},
                                                "volumes": [{"type": "volume", "source": "state", "target": "/data"}],
                                                "ports": [{"target": 8765, "published": "18765", "host_ip": "127.0.0.1"}]},
                            eng.SHADOW_SERVICE: {"ports": [{"published": "18766", "host_ip": "127.0.0.1"}]}}}
    config["volumes"] = {"state": {"name": "fixture-state"}}
    state["config"] = config

    def run(argv, **kwargs):
        argv = list(map(str, argv))
        state["calls"].append(argv)
        if argv[:2] == ["docker", "compose"]:
            if "config" in argv:
                return 0, json.dumps(config)
            if "ps" in argv:
                return 0, "fixture-daemon" if argv[-1] == up.DAEMON_SERVICE else "fixture-pg"
            if state["fail"] in ("build", "up") and state["fail"] in argv:
                return 1, "fixture failure"
            return 0, ""
        if argv[1:2] == ["inspect"]:
            return 0, json.dumps([state["running"]])
        if "psql" in argv and "-Atc" in argv:
            return 0, state["schema"]
        if "test ! -e /data/memory_state/weights.pt" in " ".join(argv):
            return int(state["pending"]), ""
        if state["fail"] == "backup" and any("backup." in a for a in argv):
            return 1, "backup failed"
        if any("backup." in a for a in argv):
            directory = repo / "data/backups"
            directory.mkdir(parents=True, exist_ok=True)
            count = state.get("backup_count", 0) + 1
            state["backup_count"] = count
            stamp = f"20260101-{count:06d}"
            (directory / f"pseudolife_memory-{stamp}.sql.gz").write_bytes(gzip.compress(b"-- PostgreSQL database dump complete\n"))
            if not state.get("state_failed"):
                with tarfile.open(directory / f"pseudolife_state-{stamp}.tgz", "w:gz") as archive:
                    member = tarfile.TarInfo("config.yaml")
                    member.size = 1
                    archive.addfile(member, io.BytesIO(b"x"))
            return 0, ""
        return 0, ""

    monkeypatch.setattr(up, "run_cli", run)
    monkeypatch.setattr(up, "docker_cmd", lambda: "docker")
    monkeypatch.setattr(up, "tree_state", lambda p: {"sha": "a" * 40, "dirty": "false", "lines": []})
    monkeypatch.setattr(up, "fetch_json", lambda *a, **kw: state["health"])
    monkeypatch.setattr(up, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "authenticated_read", lambda *a: state["read"])
    monkeypatch.setattr(up, "which", lambda name: name)
    state["repo"] = repo
    state["update"] = lambda engine="rust", **kw: up.Update(up.Options(checkout=repo, engine=engine, health_retries=1,
                                                                       daemon_url="http://127.0.0.1:18765", **kw), log=lambda s: None)
    return state


def test_activation_orders_backup_tag_build_recreate_gate_then_persists(engine_host):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    update = s["update"]()
    eng.deploy_engine(update, s["repo"])
    calls = s["calls"]
    backup = next(i for i, a in enumerate(calls) if any("backup." in x for x in a))
    tag = next(i for i, a in enumerate(calls) if a[1:2] == ["tag"])
    build = next(i for i, a in enumerate(calls) if "build" in a)
    recreate = next(i for i, a in enumerate(calls) if "up" in a)
    assert backup < tag < build < recreate
    assert calls[tag][2] == "sha256:validated-python"
    assert calls[recreate][-5:] == ["up", "-d", "--no-deps", "--no-build", up.DAEMON_SERVICE]
    assert calls[recreate][-1] == up.DAEMON_SERVICE
    assert "fixture-pg" in calls[backup] and "fixture-daemon" in calls[backup]
    selection = eng._selection(s["repo"])
    assert selection["labels"][eng.ENGINE_LABEL] == "rust"
    assert update.report.clients is None
    assert eng.selected_checkout(s["update"](None)) == s["repo"]
    rollback = json.loads(Path(update.report.rollback["overlay"]).read_text())["services"][up.DAEMON_SERVICE]
    assert rollback["labels"][eng.ENGINE_LABEL] == "python"
    assert rollback["image"] == update.report.rollback["tag"]
    assert any("--no-build" in line for line in update.rollback_lines)


@pytest.mark.parametrize("failure", ["backup", "build", "up", "read", "version", "schema"])
def test_failure_never_commits_engine_selection(engine_host, failure, monkeypatch):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    if failure == "read":
        # The running image passes; only the replacement fails its read gate.
        reads = iter([True, False])
        monkeypatch.setattr(eng, "authenticated_read", lambda *a: next(reads))
    elif failure in ("version", "schema"):
        s["health"][failure] = "wrong"
    else:
        s["fail"] = failure
    update = s["update"]()
    with pytest.raises(up.UpdateError):
        eng.deploy_engine(update, s["repo"])
    assert not (s["repo"] / eng.SELECTION).exists()
    if failure == "backup":
        assert not any(a[1:2] == ["tag"] for a in s["calls"])
    else:
        assert update.report.rollback["state"] == "tagged"
        assert Path(update.report.rollback["overlay"]).is_file()


@pytest.mark.parametrize("schema,pending", [("56", False), ("58", True), ("bad", False)])
def test_d2_stops_before_backup_or_image_movement(engine_host, schema, pending):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    s["schema"], s["pending"] = schema, pending
    with pytest.raises(up.UpdateError, match="Python"):
        eng.deploy_engine(s["update"](), s["repo"])
    assert not any("build" in a or "up" in a or a[1:2] == ["tag"] or any("backup." in x for x in a) for a in s["calls"])


def test_switch_back_to_python_uses_running_rust_for_rollback(engine_host):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    s["running"]["Image"] = "sha256:validated-rust"
    s["running"]["Config"]["Labels"] = {eng.ENGINE_LABEL: "rust"}
    update = s["update"]("python")
    eng.deploy_engine(update, s["repo"])
    assert eng._selection(s["repo"])["labels"][eng.ENGINE_LABEL] == "python"
    assert any(a[1:3] == ["tag", "sha256:validated-rust"] for a in s["calls"])


def test_shadow_missing_support_refuses_without_changes(engine_host):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    with pytest.raises(up.UpdateError, match="shadow support is missing"):
        eng.deploy_engine(s["update"](shadow=True), s["repo"])
    assert "PSEUDOLIFE_SHADOW_DB_PASSWORD" not in (s["repo"] / "ops/.env").read_text()
    assert not any("build" in a or "up" in a or a[1:2] == ["tag"] for a in s["calls"])


def test_shadow_uses_only_shadow_service_and_keeps_password_on_rerun(engine_host, monkeypatch):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    (s["repo"] / "ops/sql").mkdir()
    (s["repo"] / "ops/sql/shadow_reader_role.sql").write_text("SELECT 1;\n")
    (s["repo"] / "rust/daemon/src/config.rs").write_text("PSEUDOLIFE_READ_ONLY\n")
    s["health"]["read_only"] = True
    eng.deploy_engine(s["update"](shadow=True), s["repo"])
    saved = (s["repo"] / "ops/.env").read_bytes()
    monkeypatch.setattr(eng.secrets, "token_urlsafe", lambda *a: pytest.fail("password rotated"))
    eng.deploy_engine(s["update"](shadow=True), s["repo"])
    assert (s["repo"] / "ops/.env").read_bytes() == saved
    assert not (s["repo"] / eng.SELECTION).exists()
    assert not any(a[1:2] == ["tag"] for a in s["calls"])
    for a in s["calls"]:
        if "build" in a or "up" in a:
            assert a[-1] == eng.SHADOW_SERVICE
        assert "shadow_password" not in " ".join(a)


@pytest.mark.parametrize("changed", ["bank", "state", "port"])
def test_changed_host_identity_refuses_before_backup(engine_host, changed):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    if changed == "bank":
        s["running"]["Config"]["Env"] = ["PSEUDOLIFE_MCP_DATABASE_URL=postgresql://different-bank"]
    elif changed == "state":
        s["running"]["Mounts"][0]["Name"] = "different-state"
    else:
        s["config"]["services"][up.DAEMON_SERVICE]["ports"][0]["published"] = "28765"
    with pytest.raises(up.UpdateError):
        eng.deploy_engine(s["update"](), s["repo"])
    assert not any(any("backup." in x for x in a) for a in s["calls"])


@pytest.mark.parametrize("shell", ["powershell", "bash"])
def test_wrappers_map_engine_and_shadow_and_preserve_failure(tmp_path, shell):
    from tests.ops_harness import BASH, PWSH
    from tests.test_ops_update_wrappers import _sandbox, _env, _args
    binary = PWSH if shell == "powershell" else BASH
    if binary is None:
        pytest.skip(f"{shell} unavailable")
    root = _sandbox(tmp_path)
    argv = ([binary, "-NoProfile", "-File", str(root / "ops/update.ps1"), "-Engine", "rust", "-Shadow"]
            if shell == "powershell" else [binary, str(root / "ops/update.sh"), "--engine", "rust", "--shadow"])
    result = subprocess.run(argv, env=_env(tmp_path, 7), capture_output=True, text=True, timeout=120)
    assert result.returncode == 7, result.stderr
    args = _args(tmp_path)
    assert args[args.index("--engine") + 1] == "rust"
    assert "--shadow" in args


def test_plain_update_resolves_persisted_checkout_without_switching_engine(engine_host, monkeypatch):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    eng.deploy_engine(s["update"](), s["repo"])
    labels = {eng.ENGINE_LABEL: "rust", eng.CHECKOUT_LABEL: str(s["repo"])}
    monkeypatch.setattr(up, "run_cli", lambda *a, **kw: (0, json.dumps(labels)))
    assert eng.selected_checkout(up.Update(up.Options())) == s["repo"]
    with pytest.raises(up.UpdateError, match="checkout engine"):
        eng.selected_checkout(up.Update(up.Options(tag="0.21.0")))


def test_unattended_release_never_replaces_a_selected_rust_engine(engine_host, monkeypatch):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    eng.deploy_engine(s["update"](), s["repo"])
    labels = {eng.ENGINE_LABEL: "rust", eng.CHECKOUT_LABEL: str(s["repo"])}
    monkeypatch.setattr(up, "run_cli", lambda *a, **kw: (0, json.dumps(labels)))
    update = up.Update(up.Options(unattended=True))
    monkeypatch.setattr(update, "target_version", lambda: pytest.fail("release selection must be refused first"))
    with pytest.raises(up.UpdateError, match="checkout engine"):
        update.deploy_release()


def test_another_checkout_inherits_running_engine(engine_host, monkeypatch, tmp_path):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    other = tmp_path / "other-checkout"
    other.mkdir()
    labels = {eng.ENGINE_LABEL: "rust", eng.CHECKOUT_LABEL: str(s["repo"])}
    monkeypatch.setattr(up, "run_cli", lambda *a, **kw: (0, json.dumps(labels)))
    update = up.Update(up.Options(checkout=other))
    assert eng.selected_checkout(update) == other
    assert update._selected_engine == "rust"


def test_persisted_engine_honors_all_and_retention(engine_host, monkeypatch):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    eng.deploy_engine(s["update"](), s["repo"])
    update = s["update"](None, all_clients=True)
    completed = []
    monkeypatch.setattr(update, "clients", lambda *a: completed.append("clients"))
    monkeypatch.setattr(update, "prune_rollbacks", lambda repo, image: completed.append(image))
    monkeypatch.setattr(update, "prune_cache", lambda *a: completed.append("cache"))
    eng.deploy_engine(update, s["repo"])
    assert completed == ["clients", "pseudolife-daemon-python", "pseudolife-daemon-rust", "cache"]


def test_state_archive_failure_stops_before_tag_even_when_backup_script_exits_zero(engine_host):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    s["state_failed"] = True
    with pytest.raises(up.UpdateError, match="state backup"):
        eng.deploy_engine(s["update"](), s["repo"])
    assert not any(a[1:2] == ["tag"] or "build" in a or "up" in a for a in s["calls"])
