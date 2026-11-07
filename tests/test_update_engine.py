"""Opt-in engine selection: refusal, persistence, activation and rollback."""
import json
import os
import errno
import gzip
import io
import tarfile
import subprocess
from pathlib import Path

import pytest

from pseudolife_memory import update_cli as up

REAL_RUN_CLI = up.run_cli

def test_engine_flags_reach_options():
    options = up.options_from_args(up.build_parser().parse_args(["--engine", "rust", "--shadow"]))
    assert options.engine == "rust" and options.shadow


@pytest.mark.parametrize("engine,shadow,location", [
    (engine, shadow, location)
    for engine, shadow in [("python", False), ("rust", False), ("rust", True)]
    for location in (["host", "storage"] if shadow else ["host", "storage", "pg-tmp", "daemon-tmp"])
])
def test_disk_floor_precedes_engine_backup_build_and_shadow_changes(shadow_host, monkeypatch, engine, shadow, location):
    from types import SimpleNamespace
    from pseudolife_memory import disk_space, update_engine as eng
    s = shadow_host
    original_env = (s["repo"] / "ops/.env").read_bytes()
    if location == "host":
        monkeypatch.setattr(disk_space.shutil, "disk_usage", lambda _: SimpleNamespace(free=disk_space.MIN_FREE - 1))
    else:
        original = up.run_cli
        def run(argv, **kw):
            target = "fixture-pg" if location == "pg-tmp" else "fixture-daemon"
            if "df" in argv and (location == "storage" or (argv[2] == target and argv[-1] == "/tmp")):
                return 0, "overlay 99999999 1 1 99% /\n"
            return original(argv, **kw)
        monkeypatch.setattr(up, "run_cli", run)
    with pytest.raises(up.UpdateError, match="8 GiB"):
        eng.deploy_engine(s["update"](engine, shadow=shadow), s["repo"])
    assert (s["repo"] / "ops/.env").read_bytes() == original_env
    assert not any("build" in c or "up" in c or c[1:2] == ["tag"] or any("backup." in str(v) for v in c) for c in s["calls"])


@pytest.mark.parametrize("engine", ["python", "rust", "stopped"])
def test_engine_rechecks_space_after_backup(engine_host, monkeypatch, engine):
    from types import SimpleNamespace
    from pseudolife_memory import disk_space, update_engine as eng
    s = engine_host
    if engine == "stopped":
        update, _, _ = _stopped_handover(s, monkeypatch)
        update._selected_engine = "python"
    else:
        update = s["update"](engine)
    state = {"backed_up": False}
    def backup(self):
        state["backed_up"] = True
        self.backup_paths = {}
    monkeypatch.setattr(eng.EngineUpdate, "backup", backup)
    monkeypatch.setattr(disk_space.shutil, "disk_usage", lambda _: SimpleNamespace(
        free=disk_space.MIN_FREE - int(state["backed_up"])))
    with pytest.raises(up.UpdateError, match="8 GiB"):
        eng.deploy_engine(update, s["repo"])
    assert state["backed_up"]
    assert not any("build" in c or "up" in c or c[1:2] == ["tag"] for c in s["calls"])


def test_release_rust_is_no_longer_refused_as_unpublished(monkeypatch, capsys):
    # Without Docker the run stops at the tier check, not at a blanket refusal.
    monkeypatch.setattr(up, "which", lambda name: None)
    monkeypatch.setattr(up, "_on_shim_runtime", lambda: False)
    assert up.main(["--engine", "rust"]) == 2
    err = capsys.readouterr().err
    assert "not published" not in err and "Docker host" in err


def _release_labels(tmp_path, monkeypatch, extra_files=(), labels=None):
    ops = tmp_path / "ops"
    ops.mkdir(exist_ok=True)
    for name in ("docker-compose.yml", "docker-compose.ghcr.yml", "docker-compose.rust-ghcr.yml", ".env"):
        (ops / name).write_text("# fixture\n")
    files = [ops / "docker-compose.yml", ops / "docker-compose.ghcr.yml", *extra_files]
    labels = {"com.docker.compose.project": "pseudolife-mcp",
              "com.docker.compose.project.config_files": ",".join(map(str, files)),
              "com.docker.compose.project.working_dir": str(ops),
              "com.docker.compose.project.environment_file": str(ops / ".env"), **(labels or {})}
    monkeypatch.setattr(up, "run_cli", lambda argv, **kw: (0, json.dumps(labels)))
    return ops


@pytest.mark.parametrize("engine", ["python", "rust"])
def test_release_compose_context_drops_engine_overlays_left_by_earlier_runs(tmp_path, monkeypatch, engine):
    # Compose labels name every -f file, including the deleted candidate and
    # rollback overlays of an earlier engine run and the Rust release overlay.
    stale = [tmp_path / "update-engine/docker-compose.engine-candidate-0123.json",
             tmp_path / "update-engine/docker-compose.rollback-20261107-000000-ab.json",
             tmp_path / "ops/docker-compose.rust-ghcr.yml"]
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path / "data"))
    ops = _release_labels(tmp_path, monkeypatch, stale)
    context = up.Update(up.Options(), log=lambda s: None).compose_context(engine=engine)
    files = [context["args"][i + 1] for i, a in enumerate(context["args"]) if a == "-f"]
    assert files[:2] == [str(ops / "docker-compose.yml"), str(ops / "docker-compose.ghcr.yml")]
    # The deploy takes this package's Rust overlay, never the clone's (it may
    # be older, without the model volume); a rollback takes the running one.
    bundled = tmp_path / "data/compose/docker-compose.rust-ghcr.yml"
    assert files[2:] == ([str(bundled)] if engine == "rust" else [])
    if engine == "rust":
        assert bundled.read_bytes() == (up.BUNDLED_COMPOSE / "docker-compose.rust-ghcr.yml").read_bytes()
        assert "pseudolife_rust_models" in bundled.read_text(encoding="utf-8")
        assert context["rust_overlay"] == str(ops / "docker-compose.rust-ghcr.yml")


def test_release_rust_host_stays_on_the_release_path(tmp_path, monkeypatch):
    from pseudolife_memory import update_engine as eng
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    labels = {eng.ENGINE_LABEL: "rust", eng.ACTIVATION_LABEL: "accepted", eng.RELEASE_LABEL: "0.23.0"}
    eng._write_overlay(tmp_path / eng.RELEASE_ACCEPTED, {"image": "fixture", "labels": labels})
    monkeypatch.setattr(up, "run_cli", lambda argv, **kw: (0, json.dumps(labels)))
    update = up.Update(up.Options(tag="0.23.0", reinstall=True), log=lambda s: None)
    assert eng.selected_checkout(update) is None
    assert eng.release_engine_choice(update) == ("rust", "rust")
    update.o.engine = "python"
    assert eng.release_engine_choice(update) == ("rust", "python")


def _release_config(env=None, volumes=None):
    return {"name": "fixture", "services": {up.DAEMON_SERVICE: {
        "environment": {"PSEUDOLIFE_MCP_TOKEN": "new-bearer", **(env or {})},
        "volumes": volumes if volumes is not None else [{"type": "volume", "source": "state", "target": "/data"}],
        "ports": [{"target": 8765, "published": "18765", "host_ip": "127.0.0.1"}]}},
        "volumes": {"state": {"name": "fixture-state"}, "models": {"name": "fixture_models"}}}


def test_release_engine_reads_the_running_bank_with_the_running_bearer():
    from pseudolife_memory import update_engine as eng
    update = up.Update(up.Options(daemon_url="http://127.0.0.1:18765"), log=lambda s: None)
    release = eng.ReleaseEngineUpdate(update, "0.23.0", "0.22.1", None, "rust")
    running = {"Mounts": [{"Destination": "/data", "Name": "fixture-state"}],
               "Config": {"Env": ["PSEUDOLIFE_MCP_TOKEN=old-bearer"]}}
    assert release.verify_project(_release_config(), running) == ("new-bearer", "old-bearer")


def test_release_engine_models_volume_and_overrides():
    from pseudolife_memory import update_engine as eng
    data = {"type": "volume", "source": "state", "target": "/data"}
    models = {"type": "volume", "source": "models", "target": "/models"}
    assert eng.model_volumes(_release_config({"PSEUDOLIFE_DAEMON_MODELS_DIR": "/models"}, [data, models])) == \
        ("/models", [("--mount", "type=volume,src=fixture_models,dst=/models", "models")])
    assert eng.model_volumes(_release_config(volumes=[data])) == \
        ("/data/models", [("--mount", "type=volume,src=fixture-state,dst=/data", "state")])
    nested = {**models, "volume": {"subpath": "cache"}}
    assert eng.model_volumes(_release_config({"PSEUDOLIFE_DAEMON_MODELS_DIR": "/models"}, [nested]))[1][0][1] \
        .endswith(",volume-subpath=cache")
    bind = {"type": "bind", "source": "/srv/models", "target": "/models"}
    assert eng.model_volumes(_release_config({"PSEUDOLIFE_DAEMON_MODELS_DIR": "/models"}, [bind]))[1] == \
        [("-v", "/srv/models:/models", None)]
    assert eng.model_volumes(_release_config({"PSEUDOLIFE_DAEMON_ONNX_DIR": "/opt/onnx"})) is None
    with pytest.raises(up.UpdateError, match="no persistent volume"):
        eng.model_volumes(_release_config({"PSEUDOLIFE_DAEMON_MODELS_DIR": "/models"}, [data]))


def test_a_comma_in_a_model_volume_subpath_is_quoted_for_docker_mount_csv():
    import csv
    from pseudolife_memory import update_engine as eng
    nested = {"type": "volume", "source": "models", "target": "/models", "volume": {"subpath": 'a,"b"'}}
    spec = eng.model_volumes(_release_config({"PSEUDOLIFE_DAEMON_MODELS_DIR": "/models"}, [nested]))[1][0][1]
    # Parsed as Docker parses --mount: one CSV record of key=value fields.
    assert next(csv.reader([spec])) == ["type=volume", "src=fixture_models", "dst=/models", 'volume-subpath=a,"b"']


def test_an_open_running_daemon_is_read_with_the_configured_bearer():
    from pseudolife_memory import update_engine as eng
    update = up.Update(up.Options(daemon_url="http://127.0.0.1:18765"), log=lambda s: None)
    release = eng.ReleaseEngineUpdate(update, "0.23.0", "0.22.1", None, "rust")
    running = {"Mounts": [{"Destination": "/data", "Name": "fixture-state"}], "Config": {"Env": ["PATH=/bin"]}}
    assert release.verify_project(_release_config(), running) == ("new-bearer", "new-bearer")


def test_model_volume_is_created_with_its_driver_or_must_exist_when_external():
    from pseudolife_memory import update_engine as eng
    calls = []

    def command(args, **kw):
        calls.append(args)
        return (1, "") if args[:2] == ["volume", "inspect"] else (0, "")
    config = _release_config()
    config["volumes"]["models"] = {"name": "fixture_models", "driver": "local",
                                   "driver_opts": {"type": "none", "o": "bind", "device": "/srv/models"}}
    eng.ensure_project_volume(command, config, "models")
    create = calls[-1]
    assert create[-1] == "fixture_models" and create[create.index("--driver") + 1] == "local"
    assert "device=/srv/models" in create and "com.docker.compose.volume=models" in create
    config["volumes"]["models"] = {"name": "fixture_models", "external": True}
    with pytest.raises(up.UpdateError, match="external"):
        eng.ensure_project_volume(command, config, "models")


@pytest.mark.parametrize("script", [True, False])
def test_release_engine_refuses_a_backup_without_its_state_archive(tmp_path, monkeypatch, script):
    from pseudolife_memory import update_engine as eng
    update = up.Update(up.Options(), log=lambda s: None)
    checkout = tmp_path / "checkout"
    directory = checkout / "data/backups"
    if script:
        monkeypatch.setattr(update, "_checkout_script", lambda *a: Path("backup.sh"))

        def backup(_checkout):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "pseudolife_memory-20260101-000001.sql.gz").write_bytes(
                gzip.compress(b"-- PostgreSQL database dump complete\n"))
        monkeypatch.setattr(update, "backup", backup)
    else:
        monkeypatch.setattr(update, "_checkout_script", lambda *a: None)
        monkeypatch.setattr(update, "builtin_backup", lambda: {"dump": "x.sql.gz", "state": None})
    with pytest.raises(up.UpdateError, match="state"):
        eng.ReleaseEngineUpdate(update, "0.23.0", "0.22.1", None, "rust").backup(checkout)


def test_release_engine_refuses_a_partial_builtin_state_archive(tmp_path, monkeypatch):
    from pseudolife_memory import update_engine as eng
    update = up.Update(up.Options(), log=lambda s: None)
    partial = tmp_path / "pseudolife_state-20260101-000001.tgz"
    partial.write_bytes(gzip.compress(b"not a whole tar")[:-6])  # a copy that failed midway
    monkeypatch.setattr(update, "_checkout_script", lambda *a: None)
    monkeypatch.setattr(update, "builtin_backup", lambda: {"dump": "x.sql.gz", "state": str(partial)})
    with pytest.raises(up.UpdateError, match="state volume backup is missing or incomplete"):
        eng.ReleaseEngineUpdate(update, "0.23.0", "0.22.1", None, "rust").backup(tmp_path)


def test_engine_change_without_a_backup_is_refused_before_docker(monkeypatch, capsys):
    monkeypatch.setattr(up, "run_cli", lambda *a, **kw: pytest.fail("refusal must precede Docker"))
    assert up.main(["--engine", "rust", "--tag", "0.23.0", "--no-backup"]) == 2
    assert "engine changes require a backup" in capsys.readouterr().err


def test_engine_selection_never_moves_the_clients(monkeypatch):
    monkeypatch.setattr(up.Update, "detect_tier", lambda self: "pip")
    monkeypatch.setattr(up, "_on_shim_runtime", lambda: False)
    update = up.Update(up.Options(engine="rust"), log=lambda s: None)
    with pytest.raises(up.UpdateError, match="Docker host"):
        update._run()
    assert update.o.daemon_only


def test_release_rollback_commands_supply_the_rust_overlay_tag(tmp_path):
    from pseudolife_memory import update_engine as eng
    env = tmp_path / ".env"
    env.write_text("PSEUDOLIFE_MCP_TOKEN=x\n")
    lines = eng.rollback_commands(["-p", "pseudolife-mcp", "-f", "base.yml"], [env], tmp_path / "rollback.json",
                                  tmp_path / "accepted.json", "0.23.0")
    assert len(lines) == 2 and "PSEUDOLIFE_MCP_TOKEN" in lines[0]
    assert "PSEUDOLIFE_IMAGE_TAG" in lines[1] and "0.23.0" in lines[1]
    assert "--no-build --pull never" in lines[1] and "accepted.json" in lines[1]


def test_python_accepts_native_release_activation_and_rejects_failed_retry(tmp_path, monkeypatch):
    from pseudolife_memory import update_engine as eng
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    labels = {eng.ENGINE_LABEL: "python", eng.ACTIVATION_LABEL: "accepted", eng.RELEASE_LABEL: "0.21.1"}
    service = {"image": "fixture:0.21.1", "labels": labels}
    eng._write_overlay(tmp_path / eng.RELEASE_ACCEPTED, service)
    assert eng._validated_engine_selection(labels) == service
    for changed in ({eng.ACTIVATION_LABEL: "failed"}, {eng.RELEASE_LABEL: "0.21.2"}, {eng.ACTIVATION_LABEL: None}):
        with pytest.raises(up.UpdateError, match="rollback"):
            eng._validated_engine_selection({**labels, **changed})


def test_shadow_requires_explicit_rust(capsys):
    assert up.main(["--shadow"]) == 2
    assert "--engine rust" in capsys.readouterr().err


@pytest.mark.parametrize("engine,shadow", [("rust", False), ("rust", True), ("python", False)])
def test_engine_checkout_refuses_foreign_project_before_mutation(engine_host, capsys, engine, shadow, monkeypatch, tmp_path):
    state = engine_host
    state["config"]["name"] = "pseudolife-mcp"
    monkeypatch.setattr(up, "data_dir", lambda: tmp_path / "update-data")
    state["running"]["Config"]["Labels"] = {"com.docker.compose.project": "external-stack"}
    update = state["update"](engine, shadow=shadow)
    assert update.run() == 2
    assert "pseudolife-mcp update" in capsys.readouterr().err
    assert not any("backup." in " ".join(c) or c[1:2] == ["tag"] or "build" in c or "up" in c
                   for c in state["calls"])


def test_dirty_shadow_removal_still_checks_project_ownership(engine_host, monkeypatch, capsys, tmp_path):
    state = engine_host
    state["config"]["name"] = "pseudolife-mcp"
    monkeypatch.setattr(up, "data_dir", lambda: tmp_path / "update-data")
    state["running"]["Config"]["Labels"] = {"com.docker.compose.project": "external-stack"}
    monkeypatch.setattr(up, "tree_state", lambda _: {"sha": "a" * 40, "dirty": "true", "lines": [" M README.md"]})
    assert state["update"]("python", shadow=True).run() == 2
    assert "pseudolife-mcp update" in capsys.readouterr().err
    assert not any("rm" in c or "up" in c or "build" in c for c in state["calls"])


def test_d2_preflight_rejects_old_schema_and_pending_import():
    from pseudolife_memory.update_engine import require_imported_bank
    for schema, pending in ((56, False), (58, True), (None, False)):
        with pytest.raises(up.UpdateError, match="Python"):
            require_imported_bank(schema, pending)
    require_imported_bank(58, False)


def test_overlay_preserves_persistent_data_and_separates_shadow():
    import yaml
    root = Path(__file__).resolve().parents[1]
    active = yaml.safe_load((root / "ops/docker-compose.rust-daemon.yml").read_text(encoding="utf-8"))
    daemon = active["services"][up.DAEMON_SERVICE]
    assert daemon["build"]["dockerfile"] == "ops/Dockerfile.daemon-rust"
    assert "tmpfs" not in daemon
    shadow = yaml.safe_load((root / "ops/docker-compose.rust-shadow.yml").read_text(encoding="utf-8"))
    assert up.DAEMON_SERVICE not in shadow["services"]
    service = shadow["services"]["pseudolife-rust-shadow"]
    assert service["environment"]["PSEUDOLIFE_READ_ONLY"] == "1"
    assert "pseudolife_shadow:" in service["environment"]["PSEUDOLIFE_MCP_DATABASE_URL"]
    assert service["ports"] == ["127.0.0.1:${PSEUDOLIFE_SHADOW_PORT:-8766}:8765"]


def _render_compose(text, env):
    """Parse, then apply Compose's ${NAME}, ${NAME:-default} and
    ${NAME:?message} interpolation to string values, as Compose does."""
    import re
    import yaml

    def value(match):
        name, op, rest = match.group(1), match.group(2), match.group(3) or ""
        if env.get(name):
            return env[name]
        if op == ":?":
            raise KeyError(name)
        return rest if op == ":-" else ""

    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            # Forms this helper does not implement must not appear.
            assert "$$" not in node and not re.search(r"\$\{[A-Za-z0-9_]+(-|\?|\+|:\+)", node), node
            return re.sub(r"\$\{([A-Za-z0-9_]+)(:-|:\?)?([^}]*)\}", value, node)
        return node
    return walk(yaml.safe_load(text))


@pytest.mark.parametrize("host_env", [
    {},
    {"TZ": "Australia/Sydney", "PSEUDOLIFE_WRITER_ID": "host-writer", "PSEUDOLIFE_MCP_TOOLSET": "full",
     "PSEUDOLIFE_MCP_TIER_MAP": "claude-desktop:minimal"},
])
def test_shadow_reads_the_writers_private_state_and_presents_its_identity(host_env):
    # #1316: the Python writer runs as uid 10001 and leaves config.yaml 0600,
    # which the shadow, root with every capability dropped, could not read (a
    # crash loop). Without the writer's TZ it rendered local times in UTC.
    import re
    root = Path(__file__).resolve().parents[1]
    env = dict(host_env, PSEUDOLIFE_SHADOW_DB_PASSWORD="fixture")
    writer = _render_compose((root / "ops/docker-compose.yml").read_text(encoding="utf-8"), env)["services"][up.DAEMON_SERVICE]
    for overlay in ("ops/docker-compose.rust-shadow.yml", "pseudolife_memory/compose/docker-compose.rust-shadow.yml"):
        shadow = _render_compose((root / overlay).read_text(encoding="utf-8"), env)["services"]["pseudolife-rust-shadow"]
        for key in ("TZ", "PSEUDOLIFE_WRITER_ID", "PSEUDOLIFE_MCP_TOOLSET", "PSEUDOLIFE_MCP_TIER_MAP"):
            assert shadow["environment"].get(key) == writer["environment"][key], (overlay, key)
        uid = re.search(r"useradd --uid (\d+) --gid (\d+)", (root / "ops/Dockerfile.daemon").read_text(encoding="utf-8"))
        assert shadow["user"] == f"{uid.group(1)}:{uid.group(2)}", overlay
        assert shadow["cap_drop"] == ["ALL"] and "cap_add" not in shadow, overlay
        assert "pseudolife_data:/data:ro" in shadow["volumes"], overlay
    # A fresh shadow model volume inherits the image directory's owner.
    rust = (root / "ops/Dockerfile.daemon-rust").read_text(encoding="utf-8")
    assert f"chown {uid.group(1)}:{uid.group(2)} /models" in rust


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
                         "Config": {"Image": "python:old", "Labels": {},
                                    "Env": ["PSEUDOLIFE_MCP_TOKEN=fixture-bearer"]}}}
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
        if "df" in argv:
            return 0, "Filesystem 1024-blocks Used Available Capacity Mounted on\noverlay 99999999 1 99999998 1% /\n"
        if argv[:2] == ["docker", "compose"]:
            if "config" in argv:
                return 0, json.dumps(config)
            if "ps" in argv:
                return 0, "fixture-daemon" if argv[-1] == up.DAEMON_SERVICE else "fixture-pg"
            if "up" in argv and argv[-1] == up.DAEMON_SERVICE:
                overlay = json.loads(Path(argv[argv.index("up") - 1]).read_text())
                state["running"]["Config"]["Labels"] = overlay["services"][up.DAEMON_SERVICE]["labels"]
            if state["fail"] in ("build", "up") and state["fail"] in argv:
                return 1, "fixture failure"
            return 0, ""
        if argv[1:2] == ["inspect"]:
            if "-f" in argv:
                return 0, json.dumps(state["running"]["Config"]["Labels"])
            return 0, json.dumps([state["running"]])
        if "psql" in argv and "-Atc" in argv:
            if "FROM cms_counters" in argv[-1]:
                return 0, "t" if state.get("counters_row") else "f"
            return 0, state["schema"]
        if "test ! -e /data/memory_state/weights.pt" in " ".join(argv):
            return int(state["pending"]), ""
        if state["fail"] == "backup" and any("backup." in a for a in argv):
            return 1, "backup failed"
        if any("backup." in a for a in argv):
            output_flag = "-OutDir" if "-OutDir" in argv else "--out-dir"
            directory = Path(argv[argv.index(output_flag) + 1])
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
    monkeypatch.setattr(up.Update, "ensure_test_login", lambda self: None)
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
    with pytest.raises(up.UpdateError) as caught:
        eng.deploy_engine(update, s["repo"])
    assert not (s["repo"] / eng.SELECTION).exists()
    retry = s["update"](None)
    if failure in ("up", "read", "version", "schema"):
        with pytest.raises(up.UpdateError, match="rollback"):
            eng.selected_checkout(retry)
    else:
        assert eng.selected_checkout(retry) is None
    if failure == "backup":
        assert str(caught.value).count("to roll back:") == 0
        assert not any(a[1:2] == ["tag"] for a in s["calls"])
    else:
        assert str(caught.value).count("to roll back:") == 1
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
    eng.deploy_engine(s["update"](), s["repo"])
    s["running"]["Image"] = "sha256:validated-rust"
    update = s["update"]("python")
    eng.deploy_engine(update, s["repo"])
    assert eng._selection(s["repo"]) is None
    assert any(a[1:3] == ["tag", "sha256:validated-rust"] for a in s["calls"])
    assert eng.selected_checkout(s["update"](None)) is None
    assert eng.selected_checkout(up.Update(up.Options(tag="0.21.0", reinstall=True))) is None


def test_shadow_missing_support_refuses_without_changes(engine_host):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    with pytest.raises(up.UpdateError, match="shadow support is missing"):
        eng.deploy_engine(s["update"](shadow=True), s["repo"])
    assert "PSEUDOLIFE_SHADOW_DB_PASSWORD" not in (s["repo"] / "ops/.env").read_text()
    assert not any("build" in a or "up" in a or a[1:2] == ["tag"] for a in s["calls"])


def test_shadow_uses_only_shadow_service_and_keeps_password_on_rerun(shadow_host, monkeypatch):
    from pseudolife_memory import update_engine as eng
    s = shadow_host
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


@pytest.fixture
def shadow_host(engine_host, monkeypatch):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    repo = s["repo"]
    (repo / "ops/sql").mkdir()
    (repo / "ops/sql/shadow_reader_role.sql").write_text("BEGIN;\nSELECT :'shadow_password';\nCOMMIT;\n")
    (repo / "ops/Dockerfile.daemon-rust").write_text("FROM scratch\n")
    (repo / "rust/daemon/src/config.rs").write_text("PSEUDOLIFE_READ_ONLY\n")
    s["health"].update(read_only=True, ready=True, db="ok")
    dsn = "postgresql://fixture:fixture@pseudolife-pg:5432/fixture"
    main = s["config"]["services"][up.DAEMON_SERVICE]
    main["environment"]["PSEUDOLIFE_MCP_DATABASE_URL"] = dsn
    s["running"]["Config"]["Env"] = ["PSEUDOLIFE_MCP_DATABASE_URL=" + dsn,
                                     "PSEUDOLIFE_MCP_TOKEN=fixture-bearer"]
    shadow = s["config"]["services"][eng.SHADOW_SERVICE]
    shadow.update(image="fixture-rust:checkout", build={"args": {"GIT_SHA": "a" * 40}},
                  volumes=[{"type": "volume", "source": "state", "target": "/data", "read_only": True}],
                  environment={"PSEUDOLIFE_READ_ONLY": "1", "PSEUDOLIFE_MCP_TOKEN": "fixture-bearer"}, labels={})
    shadow["ports"][0]["target"] = 8765
    original_run = up.run_cli
    s["shadow"] = None

    def run(argv, **kw):
        args = list(map(str, argv))
        env = kw.get("env") or {}
        if "config" in args and any("rust-shadow" in a for a in args):
            shadow["environment"]["PSEUDOLIFE_MCP_DATABASE_URL"] = (
                "postgresql://pseudolife_shadow:" + env.get("PSEUDOLIFE_SHADOW_DB_PASSWORD", "missing")
                + "@pseudolife-pg:5432/fixture")
        if "ps" in args and args[-1] == eng.SHADOW_SERVICE:
            s["calls"].append(args)
            return 0, "fixture-shadow" if s["shadow"] else ""
        if args[1:] == ["inspect", "fixture-shadow"]:
            s["calls"].append(args)
            return 0, json.dumps([s["shadow"]])
        if "up" in args and args[-1] == eng.SHADOW_SERVICE and s["fail"] != "up":
            s["shadow"] = {"State": {"Running": True}, "Config": {"Labels": {
                "org.pseudolife.shadow.configuration": env.get("PSEUDOLIFE_SHADOW_CONFIGURATION")}}}
        if "rm" in args and args[-1] == eng.SHADOW_SERVICE:
            s["shadow"] = None
        return original_run(argv, **kw)

    monkeypatch.setattr(up, "run_cli", run)
    return s


def test_shadow_rerun_is_a_noop_and_python_removal_only_removes_shadow(shadow_host):
    from pseudolife_memory import update_engine as eng
    s = shadow_host
    before = json.dumps(s["running"], sort_keys=True)
    eng.deploy_engine(s["update"](shadow=True), s["repo"])
    env_file = s["repo"] / "ops/.env"
    saved = env_file.read_bytes(), env_file.stat().st_mtime_ns
    s["calls"].clear()
    eng.deploy_engine(s["update"](shadow=True), s["repo"])
    assert not any("build" in a or "up" in a or "cp" in a for a in s["calls"])
    assert (env_file.read_bytes(), env_file.stat().st_mtime_ns) == saved
    s["calls"].clear()
    eng.deploy_engine(s["update"]("python", shadow=True), s["repo"])
    assert s["shadow"] is None
    assert any("rm" in a and a[-1] == eng.SHADOW_SERVICE for a in s["calls"])
    assert not any("up" in a or "build" in a or "psql" in a for a in s["calls"])
    assert json.dumps(s["running"], sort_keys=True) == before
    assert not (s["repo"] / eng.SELECTION).exists()
    assert (env_file.read_bytes(), env_file.stat().st_mtime_ns) == saved


@pytest.mark.parametrize("missing", ["overlay", "dockerfile", "sql", "setting", "loopback", "volume", "bank", "auth"])
def test_shadow_preflight_refuses_before_password_or_mutation(shadow_host, missing):
    from pseudolife_memory import update_engine as eng
    s = shadow_host
    paths = {"overlay": "ops/docker-compose.rust-shadow.yml", "dockerfile": "ops/Dockerfile.daemon-rust",
             "sql": "ops/sql/shadow_reader_role.sql", "setting": "rust/daemon/src/config.rs"}
    if missing in paths:
        (s["repo"] / paths[missing]).unlink()
    else:
        service = s["config"]["services"][eng.SHADOW_SERVICE]
        if missing == "loopback":
            service["ports"][0]["host_ip"] = "0.0.0.0"
        elif missing == "volume":
            service["volumes"][0]["read_only"] = False
        elif missing == "bank":
            s["config"]["services"]["pseudolife-pg"]["environment"]["POSTGRES_DB"] = "another"
        else:
            service["environment"]["PSEUDOLIFE_MCP_TOKEN"] = ""
    original = (s["repo"] / "ops/.env").read_bytes()
    with pytest.raises(up.UpdateError, match="nothing was changed"):
        eng.deploy_engine(s["update"](shadow=True), s["repo"])
    assert (s["repo"] / "ops/.env").read_bytes() == original
    assert not any("build" in a or "up" in a or "cp" in a or "-f" in a and "psql" in a for a in s["calls"])


@pytest.mark.parametrize("field,value", [("read_only", False), ("ready", False), ("db", "error")])
def test_shadow_failed_gate_removes_companion_and_preserves_python(shadow_host, field, value):
    from pseudolife_memory import update_engine as eng
    s = shadow_host
    before = json.dumps(s["running"], sort_keys=True)
    s["health"][field] = value
    with pytest.raises(up.UpdateError, match="Python is unchanged"):
        eng.deploy_engine(s["update"](shadow=True), s["repo"])
    assert s["shadow"] is None
    assert json.dumps(s["running"], sort_keys=True) == before
    assert not (s["repo"] / eng.SELECTION).exists()


@pytest.mark.parametrize("phase", ["startup", "gate"])
@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit, RuntimeError])
def test_shadow_interruption_removes_companion_and_reraises(shadow_host, monkeypatch, phase, exception):
    from pseudolife_memory import update_engine as eng
    s = shadow_host
    before = json.dumps(s["running"], sort_keys=True)
    failure = exception("interrupted")
    run = up.run_cli

    def interrupt(argv, **kw):
        result = run(argv, **kw)
        if phase == "startup" and "up" in argv:
            raise failure
        return result

    def gate(*a, **kw):
        raise failure

    monkeypatch.setattr(up, "run_cli", interrupt)
    if phase == "gate":
        monkeypatch.setattr(eng.EngineUpdate, "gate", gate)
    with pytest.raises(exception) as caught:
        eng.deploy_engine(s["update"](shadow=True), s["repo"])
    assert caught.value is failure
    assert s["shadow"] is None
    assert any("rm" in a and a[-1] == eng.SHADOW_SERVICE for a in s["calls"])
    assert json.dumps(s["running"], sort_keys=True) == before


def test_shadow_password_preserves_existing_env_file_access(shadow_host):
    from pseudolife_memory import update_engine as eng
    s = shadow_host
    env_file = s["repo"] / "ops/.env"
    original = b"# shared host settings\r\nPSEUDOLIFE_MCP_TOKEN=fixture-bearer\r\n"
    env_file.write_bytes(original)
    if os.name != "nt":
        env_file.chmod(0o640)
    before = env_file.stat()
    eng.deploy_engine(s["update"](shadow=True), s["repo"])
    after = env_file.stat()
    # Keep the original file itself, including its owner, group and ACLs.
    assert after.st_ino == before.st_ino
    assert (after.st_mode, after.st_uid, after.st_gid) == (before.st_mode, before.st_uid, before.st_gid)
    assert env_file.read_bytes().startswith(original)
    assert env_file.read_text().count("PSEUDOLIFE_SHADOW_DB_PASSWORD=") == 1


@pytest.mark.parametrize("exception", [OSError, KeyboardInterrupt])
def test_shadow_failed_password_append_restores_original_bytes(shadow_host, monkeypatch, exception):
    from pseudolife_memory import update_engine as eng
    s = shadow_host
    env_file = s["repo"] / "ops/.env"
    original = env_file.read_bytes()
    inode = env_file.stat().st_ino
    fsync = os.fsync

    def interrupt(fd):
        monkeypatch.setattr(os, "fsync", fsync)
        raise exception("interrupted append")

    monkeypatch.setattr(os, "fsync", interrupt)
    with pytest.raises(up.UpdateError if exception is OSError else KeyboardInterrupt):
        eng.deploy_engine(s["update"](shadow=True), s["repo"])
    assert env_file.read_bytes() == original
    assert env_file.stat().st_ino == inode
    assert not any("up" in a or "cp" in a for a in s["calls"])


@pytest.mark.parametrize("short_write", [False, True])
def test_shadow_persistent_enospc_restores_env_without_retrying_buffered_output(shadow_host, monkeypatch, short_write):
    from pseudolife_memory import update_engine as eng
    s = shadow_host
    env_file = s["repo"] / "ops/.env"
    original = env_file.read_bytes()
    inode = env_file.stat().st_ino
    open_file = Path.open
    writes = []

    class FullDisk(io.FileIO):
        def write(self, data):
            writes.append(len(data))
            if short_write and len(writes) == 1:
                return super().write(data[:len(b"\nPSEUDOLIFE_SHADOW_DB_PASSWORD=")])
            # Persistent failure, including every attempted buffer flush.
            raise OSError(errno.ENOSPC, "synthetic full disk")

    def open_full_disk(path, mode="r", buffering=-1, **kw):
        if path == env_file and mode == "r+b":
            raw = FullDisk(path, mode)
            return raw if buffering == 0 else io.BufferedRandom(raw)
        return open_file(path, mode, buffering=buffering, **kw)

    monkeypatch.setattr(Path, "open", open_full_disk)
    with pytest.raises(up.UpdateError, match="cannot save the shadow setup files"):
        eng.deploy_engine(s["update"](shadow=True), s["repo"])
    assert env_file.read_bytes() == original
    assert env_file.stat().st_ino == inode
    assert len(writes) == (2 if short_write else 1)
    assert not any("up" in a or "cp" in a for a in s["calls"])
    # With space available again, the ordinary retry needs no file repair.
    monkeypatch.setattr(Path, "open", open_file)
    eng.deploy_engine(s["update"](shadow=True), s["repo"])
    assert s["shadow"] is not None
    assert env_file.read_text().count("PSEUDOLIFE_SHADOW_DB_PASSWORD=") == 1


@pytest.mark.parametrize("write_limit", [0, 3])
def test_shadow_raw_append_handles_short_writes_and_zero_progress(shadow_host, monkeypatch, write_limit):
    from pseudolife_memory import update_engine as eng
    s = shadow_host
    env_file = s["repo"] / "ops/.env"
    original = env_file.read_bytes()
    open_file = Path.open

    class LimitedWrite(io.FileIO):
        attempts = 0

        def write(self, data):
            self.attempts += 1
            assert write_limit or self.attempts == 1, "zero-progress append retried"
            return super().write(data[:write_limit])

    def open_limited(path, mode="r", buffering=-1, **kw):
        if path == env_file and mode == "r+b":
            assert buffering == 0
            return LimitedWrite(path, mode)
        return open_file(path, mode, buffering=buffering, **kw)

    monkeypatch.setattr(Path, "open", open_limited)
    if write_limit == 0:
        with pytest.raises(up.UpdateError, match="cannot save the shadow setup files"):
            eng.deploy_engine(s["update"](shadow=True), s["repo"])
        assert env_file.read_bytes() == original
        assert s["shadow"] is None
    else:
        eng.deploy_engine(s["update"](shadow=True), s["repo"])
        assert s["shadow"] is not None
        assert env_file.read_bytes().startswith(original)
        assert env_file.read_text().count("PSEUDOLIFE_SHADOW_DB_PASSWORD=") == 1


def test_shadow_new_env_file_is_private(shadow_host):
    from pseudolife_memory import credentials, update_engine as eng
    s = shadow_host
    env_file = s["repo"] / "ops/.env"
    env_file.unlink()
    eng.deploy_engine(s["update"](shadow=True), s["repo"])
    if os.name == "nt":
        with env_file.open("rb") as stream:
            assert credentials._windows_owner_only(stream.fileno())
    else:
        assert env_file.stat().st_mode & 0o777 == 0o600
    assert env_file.read_text().count("PSEUDOLIFE_SHADOW_DB_PASSWORD=") == 1


def test_shadow_waits_for_model_provisioning_before_authenticated_gate(shadow_host):
    from pseudolife_memory import update_engine as eng
    s = shadow_host
    eng.deploy_engine(s["update"](shadow=True), s["repo"])
    launch = next(a for a in s["calls"] if "up" in a)
    assert "--wait" in launch
    assert int(launch[launch.index("--wait-timeout") + 1]) >= 1890


def test_shadow_remove_needs_no_healthy_writer_or_read_only_sources(shadow_host):
    from pseudolife_memory import update_engine as eng
    s = shadow_host
    s["shadow"] = {"State": {"Running": False}}
    (s["repo"] / "ops/sql/shadow_reader_role.sql").unlink()
    s["health"] = None
    eng.deploy_engine(s["update"]("python", shadow=True), s["repo"])
    assert s["shadow"] is None
    assert not any("psql" in a or "up" in a or "build" in a for a in s["calls"])


def test_shadow_removal_cli_dispatches_without_post_deploy_steps(shadow_host, monkeypatch):
    from contextlib import nullcontext
    s = shadow_host
    s["shadow"] = {"State": {"Running": False}}
    monkeypatch.setattr(up, "UpdateLock", nullcontext)
    monkeypatch.setattr(up.Update, "checkout_post_deploy", lambda *a: pytest.fail("writer post-deploy"))
    monkeypatch.setattr(up.Update, "maintainer_passkeys", lambda *a: pytest.fail("interactive setup"))
    assert up.main(["--engine", "python", "--shadow", "--checkout", str(s["repo"])]) == 0
    assert s["shadow"] is None


@pytest.mark.parametrize("saved_password", [False, True])
def test_shadow_build_preserves_intervening_env_edit(shadow_host, monkeypatch, saved_password):
    from pseudolife_memory import update_engine as eng
    s = shadow_host
    env_file = s["repo"] / "ops/.env"
    if saved_password:
        env_file.write_text("PSEUDOLIFE_SHADOW_DB_PASSWORD=existing-password\n")
    edited = env_file.read_text() + "PSEUDOLIFE_MCP_TOKEN=rotated-during-build\n"
    original_run = up.run_cli

    def run(argv, **kw):
        if "build" in argv:
            env_file.write_text(edited)
        return original_run(argv, **kw)

    monkeypatch.setattr(up, "run_cli", run)
    with pytest.raises(up.UpdateError, match="host env file changed.*retry"):
        eng.deploy_engine(s["update"](shadow=True), s["repo"])
    assert env_file.read_text() == edited
    assert s["shadow"] is None
    assert not any("cp" in a or "up" in a for a in s["calls"])


def test_shadow_build_failure_does_not_save_password(shadow_host):
    from pseudolife_memory import update_engine as eng
    s = shadow_host
    s["fail"] = "build"
    original = (s["repo"] / "ops/.env").read_bytes()
    with pytest.raises(up.UpdateError, match="build failed; Python is unchanged"):
        eng.deploy_engine(s["update"](shadow=True), s["repo"])
    assert (s["repo"] / "ops/.env").read_bytes() == original
    assert s["shadow"] is None


def test_shadow_role_failure_names_fix_and_never_starts_companion(shadow_host, monkeypatch):
    from pseudolife_memory import update_engine as eng
    s = shadow_host
    run = up.run_cli

    def fail_role(argv, **kw):
        if "psql" in argv and "-f" in argv:
            return 1, "private server diagnostics must not be printed"
        return run(argv, **kw)

    monkeypatch.setattr(up, "run_cli", fail_role)
    with pytest.raises(up.UpdateError, match="SELECT-only shadow role.*Python is unchanged") as failure:
        eng.deploy_engine(s["update"](shadow=True), s["repo"])
    assert "private server" not in str(failure.value)
    assert not any("up" in a for a in s["calls"])
    assert s["shadow"] is None


@pytest.mark.parametrize("changed", ["bank", "state", "port"])
def test_changed_host_identity_refuses_before_backup(engine_host, changed):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    if changed == "bank":
        s["running"]["Config"]["Env"] = ["PSEUDOLIFE_MCP_DATABASE_URL=postgresql://different-bank",
                                         "PSEUDOLIFE_MCP_TOKEN=fixture-bearer"]
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
    labels = dict(s["running"]["Config"]["Labels"])
    monkeypatch.setattr(up, "run_cli", lambda *a, **kw: (0, json.dumps(labels)))
    assert eng.selected_checkout(up.Update(up.Options())) == s["repo"]
    with pytest.raises(up.UpdateError, match="checkout engine"):
        eng.selected_checkout(up.Update(up.Options(tag="0.21.0")))


def test_unattended_release_never_replaces_a_selected_rust_engine(engine_host, monkeypatch):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    eng.deploy_engine(s["update"](), s["repo"])
    labels = dict(s["running"]["Config"]["Labels"])
    monkeypatch.setattr(up, "run_cli", lambda *a, **kw: (0, json.dumps(labels)))
    update = up.Update(up.Options(unattended=True))
    monkeypatch.setattr(update, "target_version", lambda: pytest.fail("release selection must be refused first"))
    with pytest.raises(up.UpdateError, match="checkout engine"):
        update.deploy_release()


def test_another_checkout_inherits_running_engine(engine_host, monkeypatch, tmp_path):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    eng.deploy_engine(s["update"](), s["repo"])
    other = tmp_path / "other-checkout"
    other.mkdir()
    labels = dict(s["running"]["Config"]["Labels"])
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
    assert completed == ["pseudolife-daemon-python", "pseudolife-daemon-rust", "clients", "cache"]


def test_state_archive_failure_stops_before_tag_even_when_backup_script_exits_zero(engine_host):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    s["state_failed"] = True
    with pytest.raises(up.UpdateError, match="state backup"):
        eng.deploy_engine(s["update"](), s["repo"])
    assert not any(a[1:2] == ["tag"] or "build" in a or "up" in a for a in s["calls"])


def test_colon_bearer_and_running_version_are_preserved(engine_host, monkeypatch):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    s["config"]["services"][up.DAEMON_SERVICE]["environment"] = {"PSEUDOLIFE_MCP_TOKENS": "fixture:colon:reader"}
    s["running"]["Config"]["Env"] = ["PSEUDOLIFE_MCP_TOKENS=fixture:colon:reader"]
    seen = []
    monkeypatch.setattr(eng, "authenticated_read", lambda url, token: seen.append(token) or True)
    update = s["update"]()
    monkeypatch.setattr(update, "daemon_health", lambda: {"status": "ok", "version": "0.20.7", "schema": 58})
    eng.deploy_engine(update, s["repo"])
    assert seen == ["fixture:colon", "fixture:colon"]
    assert update.report.rollback["tag"].startswith("pseudolife-daemon-python:0.20.7-")


def test_engine_runs_normal_post_deploy_steps(engine_host, monkeypatch):
    s = engine_host
    update = s["update"]()
    completed = []
    monkeypatch.setattr(up, "UpdateLock", __import__("contextlib").nullcontext)
    monkeypatch.setattr(update, "hooks_changed_note", lambda *a: completed.append("hooks"))
    monkeypatch.setattr(update, "ensure_test_login", lambda: completed.append("login"))
    monkeypatch.setattr(update, "maintainer_passkeys", lambda: completed.append("passkeys"))
    update._run()
    assert completed == ["hooks", "login", "passkeys"]


@pytest.mark.parametrize("docker_exit", [0, 7])
@pytest.mark.parametrize("previously_activated", [False, True])
def test_printed_python_rollback_clears_selection_only_after_recreate(engine_host, monkeypatch, docker_exit, previously_activated):
    import os
    from tests.ops_harness import BASH, PWSH
    from pseudolife_memory import update_engine as eng
    s = engine_host
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "fixture-bearer")
    if previously_activated:
        eng.deploy_engine(s["update"](), s["repo"])
        eng.deploy_engine(s["update"]("python"), s["repo"])
    update = s["update"]()
    eng.deploy_engine(update, s["repo"])
    binary = PWSH if os.name == "nt" else BASH
    if binary is None:
        pytest.skip("native rollback shell unavailable")
    commands = "\n".join(line for line in update.rollback_lines if not line.startswith("      "))
    if os.name == "nt":
        script = s["repo"] / "rollback.ps1"
        script.write_text(f"function docker {{ $global:LASTEXITCODE = {docker_exit} }}\n" + commands)
        argv = [binary, "-NoProfile", "-File", str(script)]
    else:
        script = s["repo"] / "rollback.sh"
        script.write_text(f"docker() {{ return {docker_exit}; }}\n" + commands)
        argv = [binary, str(script)]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    assert (s["repo"] / eng.SELECTION).exists() == (docker_exit != 0), result.stderr
    if docker_exit == 0:
        assert result.returncode == 0, result.stderr
        rollback = json.loads(Path(update.report.rollback["overlay"]).read_text())
        s["running"]["Config"]["Labels"] = rollback["services"][up.DAEMON_SERVICE]["labels"]
        assert eng.selected_checkout(up.Update(up.Options(tag="0.21.0"))) is None


@pytest.mark.parametrize("force", [False, True])
def test_force_rollback_tag_uses_validated_running_image(engine_host, monkeypatch, force):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    run = up.run_cli
    def collision(argv, **kw):
        if list(argv)[1:3] == ["image", "inspect"]:
            return 0, "sha256:another-rollback"
        return run(argv, **kw)
    monkeypatch.setattr(up, "run_cli", collision)
    update = s["update"](force_rollback_tag=force)
    if not force:
        with pytest.raises(up.UpdateError, match="--force-rollback-tag"):
            eng.deploy_engine(update, s["repo"])
        assert not any(a[1:2] == ["tag"] for a in s["calls"])
    else:
        eng.deploy_engine(update, s["repo"])
        assert any(a[1:3] == ["tag", "sha256:validated-python"] for a in s["calls"])


def test_retention_preserves_this_runs_old_image_rollback(engine_host, monkeypatch):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    refs = {"pseudolife-daemon-python:0.21.0-pre-newer1": ("2026-10-02", "sha256:newer1"),
            "pseudolife-daemon-python:0.21.0-pre-newer2": ("2026-10-03", "sha256:newer2")}
    run = up.run_cli
    def docker(argv, **kw):
        args = list(map(str, argv))
        if args[1:2] == ["tag"]:
            refs[args[3]] = ("2026-10-01", args[2])
        if args[1:3] == ["ps", "-q"]:
            return 0, "fixture-daemon"
        if args[1:3] == ["inspect", "--format"]:
            return 0, s["running"]["Image"]
        if args[1:3] == ["image", "ls"]:
            return 0, "\n".join(r for r in refs if r.startswith(args[3] + ":"))
        if args[1:4] == ["image", "inspect", "--format"]:
            return 0, "|".join(refs[args[-1]])
        if args[1:2] == ["rmi"]:
            del refs[args[2]]
            return 0, ""
        result = run(argv, **kw)
        if args[1:2] == ["compose"] and "up" in args:
            s["running"]["Image"] = "sha256:new-rust"
        return result
    monkeypatch.setattr(up, "run_cli", docker)
    update = s["update"](keep_rollbacks=2)
    eng.deploy_engine(update, s["repo"])
    assert update.report.rollback["tag"] in refs
    assert refs[update.report.rollback["tag"]][1] == "sha256:validated-python"


def test_stale_checkout_selection_refuses_after_other_checkout_returns_python(engine_host, tmp_path):
    import shutil
    from pseudolife_memory import update_engine as eng
    s = engine_host
    eng.deploy_engine(s["update"](), s["repo"])
    other = tmp_path / "checkout-b"
    shutil.copytree(s["repo"], other)
    update = s["update"]("python")
    update.o.checkout = other
    eng.deploy_engine(update, other)
    assert eng._selection(s["repo"])["labels"][eng.ENGINE_LABEL] == "rust"
    with pytest.raises(up.UpdateError, match="--engine python"):
        eng.selected_checkout(s["update"](None))


def test_compose_json_ignores_stderr_warning(engine_host, monkeypatch):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    engine = eng.EngineUpdate(s["update"](), s["repo"])
    # Exercise the real command capture, not a JSON-normalizing mock.
    monkeypatch.setattr(up, "run_cli", REAL_RUN_CLI)
    monkeypatch.setattr(up.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a[0], 0,
                        json.dumps(s["config"]), "warning: version is obsolete\n"))
    assert engine.config(engine.base) == s["config"]


@pytest.mark.parametrize("raw,expected", [
    (" malformed, :reader, reserved:default,  fixture:colon : Reader , next:other ", "fixture:colon"),
    ("  fixture inner : Reader  ", "fixture inner"),
])
def test_mapped_bearer_matches_daemon_whitespace_and_malformed_entries(engine_host, monkeypatch, raw, expected):
    from pseudolife_memory import update_engine as eng
    from pseudolife_memory.principals import env_auth, presented_bearer
    s = engine_host
    env = {"PSEUDOLIFE_MCP_TOKENS": raw}
    s["config"]["services"][up.DAEMON_SERVICE]["environment"] = env
    s["running"]["Config"]["Env"] = ["PSEUDOLIFE_MCP_TOKENS=" + raw]
    tokens, _ = env_auth(env)
    assert expected in tokens and presented_bearer("Bearer " + expected) == expected
    seen = []
    monkeypatch.setattr(eng, "authenticated_read", lambda url, token: seen.append(token) or token in tokens)
    eng.deploy_engine(s["update"](), s["repo"])
    assert seen == [expected, expected]


@pytest.mark.parametrize("failure", ["version", "schema", "counters_import_required"])
@pytest.mark.parametrize("retry_engine", ["rust", "python"])
@pytest.mark.parametrize("already_rust", [False, True])
def test_explicit_retry_cannot_tag_failed_activation(engine_host, monkeypatch, failure, retry_engine, already_rust):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    if already_rust:
        eng.deploy_engine(s["update"](), s["repo"])
    s["health"][failure] = True if failure == "counters_import_required" else "wrong"
    first = s["update"]()
    with pytest.raises(up.UpdateError, match="to roll back:"):
        eng.deploy_engine(first, s["repo"])
    rollback = Path(first.report.rollback["overlay"])
    original = rollback.read_bytes()
    # Even a subsequently healthy read cannot validate a failed deployment.
    s["health"] = {"status": "ok", "version": "0.21.0", "schema": 58}
    s["calls"].clear()
    monkeypatch.setattr(up, "UpdateLock", __import__("contextlib").nullcontext)
    with pytest.raises(up.UpdateError, match="Python rollback"):
        s["update"](retry_engine)._run()
    assert rollback.read_bytes() == original
    assert not any(a[1:2] in (["tag"], ["rmi"]) or "build" in a or "up" in a
                   or any("backup." in x or "prune-" in x for x in a) for a in s["calls"])


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_selection_write_error_reports_rollback_once(engine_host, monkeypatch, cleanup_fails):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    write = eng._write_overlay
    def fail_selection(path, service):
        if path == s["repo"] / eng.SELECTION:
            raise OSError("fixture selection write failed")
        return write(path, service)
    monkeypatch.setattr(eng, "_write_overlay", fail_selection)
    if cleanup_fails:
        unlink = Path.unlink
        def fail_cleanup(path, *args, **kw):
            if path.name == "docker-compose.engine-candidate.json":
                raise OSError("fixture cleanup failed")
            return unlink(path, *args, **kw)
        monkeypatch.setattr(Path, "unlink", fail_cleanup)
    update = s["update"]()
    with pytest.raises(up.UpdateError) as caught:
        eng.deploy_engine(update, s["repo"])
    assert "fixture selection write failed" in str(caught.value)
    assert str(caught.value).count("to roll back:") == 1
    assert all(line in str(caught.value) for line in update.rollback_lines)
    assert update.report.rollback["tag"].startswith("pseudolife-daemon-python:")
    assert not (s["repo"] / eng.SELECTION).exists()


@pytest.mark.parametrize("checkout", [False, True])
def test_plain_python_update_accepts_null_container_labels(engine_host, monkeypatch, checkout):
    s = engine_host
    s["running"]["Config"]["Labels"] = None
    update = s["update"](None)
    if not checkout:
        update.o.checkout = None
    completed = []
    monkeypatch.setattr(up, "UpdateLock", __import__("contextlib").nullcontext)
    monkeypatch.setattr(update, "detect_tier", lambda: "docker")
    monkeypatch.setattr(update, "deploy_checkout", lambda: completed.append("checkout"))
    monkeypatch.setattr(update, "deploy_release", lambda: completed.append("release"))
    monkeypatch.setattr(update, "maintainer_passkeys", lambda: None)
    update._run()
    assert completed == ["checkout" if checkout else "release"]
    assert any("{{json .Config.Labels}}" in a for a in s["calls"])


def test_first_activation_accepts_null_config_labels(engine_host):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    s["running"]["Config"]["Labels"] = None
    update = s["update"]()
    eng.deploy_engine(update, s["repo"])
    assert update.report.rollback["tag"].startswith("pseudolife-daemon-python:")
    assert eng._selection(s["repo"])["labels"][eng.ENGINE_LABEL] == "rust"


def test_imported_counters_row_allows_unrenamed_weights_file(engine_host):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    s["pending"] = True
    s["counters_row"] = True
    eng.deploy_engine(s["update"](), s["repo"])
    assert eng._selection(s["repo"])["labels"][eng.ENGINE_LABEL] == "rust"


@pytest.mark.parametrize("mode,args", [
    ("lease", ["break", "fixture-lease"]),
    ("maintainer", ["list"]),
    ("board-audit", ["export"]),
    ("invite", ["fixture-machine"]),
    ("invite", ["fixture-machine", "--json"]),
])
def test_operator_after_rust_activation_uses_the_native_cli(engine_host, monkeypatch, capsys, mode, args):
    from pseudolife_memory import (board_audit_cli, daemon_exec, invite_cli,
                                   lease_cli, maintainer_cli, maintainer_setup, update_engine as eng)
    s = engine_host
    eng.deploy_engine(s["update"](), s["repo"])
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.delenv(daemon_exec.NESTED_ENV, raising=False)
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(s["repo"] / "empty-bank"))
    monkeypatch.setenv("PSEUDOLIFE_DOCKER", "fixture-docker")
    monkeypatch.setattr(invite_cli, "local_database", lambda: (None, None))
    monkeypatch.setattr(invite_cli, "docker_available", lambda: True)
    monkeypatch.setattr(maintainer_setup, "health", lambda url: {"status": "ok", "auth": True})
    commands = []
    def docker(argv, **kw):
        commands.append(argv)
        if argv[1] == "inspect":
            value = "true" if ".State.Running" in argv[3] else s["running"]["Config"]["Labels"][eng.ENGINE_LABEL]
            return subprocess.CompletedProcess(argv, 0, value, "")
        if "--version-check" in argv:
            return subprocess.CompletedProcess(argv, 0, "pseudolife-mcp invite 1\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    for module in (daemon_exec, invite_cli, maintainer_setup):
        monkeypatch.setattr(module, "run", docker)
    main = {"lease": lease_cli.main, "maintainer": maintainer_cli.main,
            "board-audit": board_audit_cli.main, "invite": invite_cli.main}[mode]
    assert main(args) == 0
    executions = [argv for argv in commands if argv[1] == "exec"]
    assert executions
    for argv in executions:
        index = argv.index(daemon_exec.CONTAINER)
        assert argv[index + 1:index + 3] == ["pseudolife-mcp", mode]
        assert f"{daemon_exec.NESTED_ENV}=1" in argv


@pytest.mark.parametrize("returned_from_rust", [False, True])
def test_python_operator_path_remains_available(engine_host, monkeypatch, returned_from_rust):
    from pseudolife_memory import daemon_exec, maintainer_cli, update_engine as eng
    s = engine_host
    if returned_from_rust:
        eng.deploy_engine(s["update"](), s["repo"])
        eng.deploy_engine(s["update"]("python"), s["repo"])
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.delenv(daemon_exec.NESTED_ENV, raising=False)
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(s["repo"] / "empty-bank"))
    monkeypatch.setenv("PSEUDOLIFE_DOCKER", "fixture-docker")
    commands = []
    def docker(argv, **kw):
        commands.append(argv)
        value = s["running"]["Config"]["Labels"].get(eng.ENGINE_LABEL, "<no value>")
        if argv[1] == "inspect" and ".State.Running" in argv[3]:
            value = "true"
        return subprocess.CompletedProcess(argv, 0, value, "")
    monkeypatch.setattr(daemon_exec, "run", docker)
    assert maintainer_cli.main(["list"]) == 0
    assert commands[-1][-3:] == ["pseudolife_memory.cli", "maintainer", "list"]
    assert commands[-1][1] == "exec"


@pytest.mark.parametrize("helper,args,tail", [
    ("host", ("state",), ["maintainer", "setup", "--host-state"]),
    ("host", ("write", "localhost", "http://localhost:8765"),
     ["maintainer", "setup", "--host-write", "localhost", "http://localhost:8765"]),
    ("maintainer", ("enrol-code",), ["maintainer", "enrol-code"]),
])
def test_setup_after_rust_activation_uses_the_native_cli(engine_host, monkeypatch, helper, args, tail):
    from pseudolife_memory import maintainer_setup, update_engine as eng

    state = engine_host
    eng.deploy_engine(state["update"](), state["repo"])
    monkeypatch.setenv("PSEUDOLIFE_DOCKER", "fixture-docker")
    commands = []

    def docker(argv, **kwargs):
        commands.append(argv)
        output = (state["running"]["Config"]["Labels"][eng.ENGINE_LABEL]
                  if argv[1] == "inspect" else '{"fixture": true}')
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(maintainer_setup, "run", docker)
    expected = ["fixture-docker", "exec", maintainer_setup.DAEMON_CONTAINER, "pseudolife-mcp", *tail]
    if helper == "host":
        assert maintainer_setup._host("docker", *args) == {"fixture": True}
        assert commands[-1] == expected
    else:
        assert maintainer_setup._maintainer("docker", *args) == expected


@pytest.mark.parametrize("docker_exit", [0, 7])
@pytest.mark.parametrize("auth_source", ["shell", "env-file"])
def test_rust_rollback_commits_selection_only_after_recreate(engine_host, monkeypatch, docker_exit, auth_source):
    import os
    from tests.ops_harness import BASH, PWSH
    from pseudolife_memory import update_engine as eng
    s = engine_host
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "fixture-bearer")
    if auth_source == "env-file":
        (s["repo"] / "ops/.env").write_text("PSEUDOLIFE_MCP_TOKEN=fixture-bearer\n")
    eng.deploy_engine(s["update"](), s["repo"])
    update = s["update"]()
    eng.deploy_engine(update, s["repo"])
    selected = s["repo"] / eng.SELECTION
    accepted = selected.read_bytes()
    during, arguments = s["repo"] / "during.json", s["repo"] / "arguments.txt"
    commands = "\n".join(line for line in update.rollback_lines if not line.startswith("      "))
    if os.name == "nt":
        if not PWSH:
            pytest.skip("PowerShell unavailable")
        script = s["repo"] / "rollback.ps1"
        script.write_text(f"function docker {{ Copy-Item -LiteralPath '{selected}' -Destination '{during}'; "
                          f"$args | Set-Content -LiteralPath '{arguments}'; $global:LASTEXITCODE = {docker_exit} }}\n" + commands)
        argv = [PWSH, "-NoProfile", "-File", str(script)]
    else:
        if not BASH:
            pytest.skip("Bash unavailable")
        import shlex
        script = s["repo"] / "rollback.sh"
        script.write_text(f"docker() {{ cp -- {shlex.quote(str(selected))} {shlex.quote(str(during))}; "
                          f"printf '%s\\n' \"$@\" > {shlex.quote(str(arguments))}; return {docker_exit}; }}\n" + commands)
        argv = [BASH, str(script)]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    assert during.read_bytes() == accepted, "the accepted selection must not change before recreation"
    args = arguments.read_text().splitlines()
    assert args[max(i for i, value in enumerate(args) if value == "-f") + 1] == update.report.rollback["overlay"]
    if docker_exit:
        assert result.returncode != 0
        assert selected.read_bytes() == accepted
    else:
        assert result.returncode == 0, result.stderr
        assert selected.read_bytes() == Path(update.report.rollback["overlay"]).read_bytes()
        s["running"]["Config"]["Labels"] = eng._selection(s["repo"])["labels"]
    assert eng.selected_checkout(s["update"](None)) == s["repo"]


@pytest.mark.parametrize("retry_engine", [None, "python", "rust"])
@pytest.mark.parametrize("failure", ["version", "schema", "counters_import_required"])
def test_failed_python_replacement_never_becomes_rollback(engine_host, monkeypatch, retry_engine, failure):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    eng.deploy_engine(s["update"](), s["repo"])
    accepted = (s["repo"] / eng.SELECTION).read_bytes()
    s["health"][failure] = True if failure == "counters_import_required" else "wrong"
    with pytest.raises(up.UpdateError, match="to roll back:"):
        eng.deploy_engine(s["update"]("python"), s["repo"])
    s["health"] = {"status": "ok", "version": "0.21.0", "schema": 58}
    s["calls"].clear()
    monkeypatch.setattr(up, "UpdateLock", __import__("contextlib").nullcontext)
    with pytest.raises(up.UpdateError, match="rollback"):
        s["update"](retry_engine)._run()
    assert (s["repo"] / eng.SELECTION).read_bytes() == accepted
    assert not any(a[1:2] in (["tag"], ["rmi"]) or "build" in a or "up" in a
                   or any("backup." in x or "prune-" in x for x in a) for a in s["calls"])


@pytest.mark.parametrize("credential", ["PSEUDOLIFE_MCP_TOKEN", "PSEUDOLIFE_MCP_TOKENS"])
@pytest.mark.parametrize("source", ["shell", "env-file", "missing-shell"])
def test_printed_rollback_preserves_http_auth(engine_host, monkeypatch, credential, source):
    import os
    import sys
    from tests.ops_harness import BASH, PWSH
    from tests.asgi_helpers import call, stub_mcp
    from pseudolife_memory.web.api import build_console_app
    from pseudolife_memory.web.fixtures import FixtureService
    from pseudolife_memory.principals import env_auth
    from pseudolife_memory import update_engine as eng
    s = engine_host
    value = "fixture-auth" + (":reader" if credential.endswith("TOKENS") else "")
    s["config"]["services"][up.DAEMON_SERVICE]["environment"] = {credential: value}
    s["running"]["Config"]["Env"] = [credential + "=" + value]
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKENS", raising=False)
    monkeypatch.setenv(credential, value if source != "env-file" else "wrong-inherited-value")
    if source == "env-file":
        (s["repo"] / "ops/.env").write_text(credential + "=" + value + "\n")
    update = s["update"]()
    eng.deploy_engine(update, s["repo"])
    # A stub Docker command captures only the effective fixture auth that
    # Compose would pass; the real daemon HTTP gate verifies that result.
    snapshot = s["repo"] / "auth.json"
    capture = s["repo"] / "capture.py"
    capture.write_text("import json,os,pathlib\n"
                       "keys=('PSEUDOLIFE_MCP_TOKEN','PSEUDOLIFE_MCP_TOKENS')\n"
                       "env={k:os.environ.get(k,'') for k in keys}\n"
                       "for line in pathlib.Path('ops/.env').read_text().splitlines():\n"
                       " if '=' in line and line.split('=',1)[0] in keys:\n"
                       "  k,v=line.split('=',1)\n"
                       "  env[k]=os.environ.get(k,v)\n"
                       "pathlib.Path('auth.json').write_text(json.dumps(env))\n")
    commands = "\n".join(line for line in update.rollback_lines if not line.startswith("      "))
    if os.name == "nt":
        if not PWSH:
            pytest.skip("PowerShell unavailable")
        script = s["repo"] / "rollback.ps1"
        script.write_text(f"function docker {{ & '{sys.executable}' '{capture}'; $global:LASTEXITCODE = 0 }}\n" + commands)
        argv = [PWSH, "-NoProfile", "-File", str(script)]
    else:
        if not BASH:
            pytest.skip("Bash unavailable")
        import shlex
        script = s["repo"] / "rollback.sh"
        script.write_text(f"docker() {{ {shlex.quote(sys.executable)} {shlex.quote(str(capture))}; }}\n" + commands)
        argv = [BASH, str(script)]
    environment = dict(os.environ)
    if source == "missing-shell":
        environment.pop("PSEUDOLIFE_MCP_TOKEN", None)
        environment.pop("PSEUDOLIFE_MCP_TOKENS", None)
    result = subprocess.run(argv, cwd=s["repo"], env=environment, capture_output=True, text=True, timeout=30)
    if source == "missing-shell":
        assert result.returncode != 0
        assert not snapshot.exists(), "rollback must refuse before Docker can start without auth"
        assert not (s["repo"] / eng.PYTHON_ACCEPTED).exists()
        assert "Restore PSEUDOLIFE_MCP_TOKEN" in result.stderr
        return
    assert result.returncode == 0, result.stderr
    tokens, single = env_auth(json.loads(snapshot.read_text()))
    app = build_console_app(stub_mcp, single, lambda: {}, FixtureService(), token_map=tokens)
    assert call(app, "POST", "/mcp")[0] == 401
    # 501 is the mounted MCP stub: authentication allowed it through.
    assert call(app, "POST", "/mcp", headers=[(b"authorization", b"Bearer fixture-auth")])[0] == 501

def test_stopped_checkout_handover_accepts_only_its_explicit_env_file(engine_host, monkeypatch):
    s = engine_host
    (s['repo'] / 'ops/.env').write_text('PSEUDOLIFE_MCP_TOKEN=fixture-carried\n')
    update = s['update'](None, allow_auth_change=True, daemon_only=True,
                         env_file=s['repo'] / 'ops/.env')
    monkeypatch.setattr(up.Update, 'detect_tier', lambda self: (_ for _ in ()).throw(RuntimeError('validated checkout handover')))
    with pytest.raises(RuntimeError, match='validated checkout handover'):
        update._run()

@pytest.mark.parametrize("invalid", ["env-file", "no-backup", "engine", "shadow", "all-clients", "unattended", "schedule", "check", "tag", "reinstall", "no-declarations"])
def test_checkout_handover_rejects_ambiguous_authority_before_discovery(engine_host, monkeypatch, invalid):
    s = engine_host
    env = s["repo"] / "ops/.env"
    env.write_text("PSEUDOLIFE_MCP_TOKEN=fixture-carried\n")
    options = dict(allow_auth_change=True, daemon_only=True, env_file=env)
    if invalid == "env-file":
        other = s["repo"] / "ops/other.env"
        other.write_text(env.read_text())
        options["env_file"] = other
    elif invalid == "no-declarations":
        env.write_text("# no declared bearer settings\n")
    elif invalid == "engine":
        options["engine"] = "python"
    else:
        options[invalid.replace("-", "_")] = "07:05" if invalid == "schedule" else "0.21.0" if invalid == "tag" else True
    engine = options.pop("engine", None)
    monkeypatch.setattr(up.Update, "detect_tier", lambda self: pytest.fail("must refuse before discovery"))
    with pytest.raises(up.UpdateError, match="--allow-auth-change requires"):
        s["update"](engine, **options)._run()
    assert not s["calls"]


@pytest.mark.parametrize("checkout", [False, True])
def test_checkout_handover_capability_is_exact_and_read_only(monkeypatch, capsys, checkout, tmp_path):
    monkeypatch.setattr(up.Update, "detect_tier", lambda _: pytest.fail("capability probe must not inspect Docker"))
    args = ["--checkout", str(tmp_path)] if checkout else []
    assert up.main([*args, "--checkout-handover-capability"]) == 0
    assert capsys.readouterr().out == "pseudolife-mcp checkout-handover-v1\n"


def _stopped_handover(s, monkeypatch, *, engine="python", stored=False):
    """Track the stopped-backup dependency through the existing Docker seam."""
    from contextlib import contextmanager
    from types import SimpleNamespace
    import sys
    from pseudolife_memory import update_engine as eng
    from pseudolife_memory.principals import secret_sha256
    s["running"]["State"]["Running"] = False
    s["running"]["NetworkSettings"] = {"Networks": {"handover": {}}}
    s["running"]["Config"]["Env"].append("PSEUDOLIFE_MCP_DATABASE_URL=postgresql://fixture:fixture@pseudolife-pg:5432/fixture")
    s["config"]["services"][up.DAEMON_SERVICE]["environment"].update(
        PSEUDOLIFE_MCP_TOKEN="" if stored else "fixture-carried",
        PSEUDOLIFE_MCP_TOKENS="",
        PSEUDOLIFE_MCP_DATABASE_URL="postgresql://fixture:fixture@pseudolife-pg:5432/fixture")
    s["config"]["volumes"]["state"]["name"] = "legacy_state_volume"
    s["running"]["Mounts"][0]["Name"] = "legacy_state_volume"
    env = s["repo"] / "ops/.env"
    env.write_text("PSEUDOLIFE_MCP_TOKEN=" + ("" if stored else "fixture-carried") + "\nPSEUDOLIFE_MCP_TOKENS=\n")
    override = s["repo"] / "ops/docker-compose.override.yml"
    override.write_text("# retained custom host configuration\n")
    shadow = s["repo"] / "data/update-engine/docker-compose.shadow.json"
    shadow.parent.mkdir(parents=True)
    shadow.write_text("fixture-shadow\n")
    if engine == "rust":
        labels = {eng.ENGINE_LABEL: "rust", eng.CHECKOUT_LABEL: str(s["repo"]), eng.ACTIVATION_LABEL: "accepted"}
        s["running"]["Config"]["Labels"] = labels
        eng._write_overlay(s["repo"] / eng.SELECTION, {"image": "fixture-rust:accepted", "labels": labels})
    old_run = up.run_cli

    def run(argv, **kw):
        if list(argv)[1:3] == ["image", "inspect"] and argv[-1] == s["running"]["Image"]:
            s["calls"].append(list(argv))
            return 0, s["running"]["Image"]
        if list(argv)[1:2] == ["inspect"] and argv[-1] == "fixture-pg":
            s["calls"].append(list(argv))
            return 0, json.dumps([{"State": {"Running": True}, "Name": "/fixture-pg",
                                  "Config": {"Env": ["POSTGRES_USER=initial", "POSTGRES_DB=initial"]},
                                  "NetworkSettings": {"Networks": {"handover": {"Aliases": ["pseudolife-pg"]}}}}])
        if "psql" in argv and "token_hash" in argv[-1]:
            s["calls"].append(list(argv))
            return 0, secret_sha256("fixture-stored")
        return old_run(argv, **kw)

    class StoppedBank:
        def __init__(self, **kw):
            assert kw["container"] == "fixture-pg" and kw["daemon_container"] == "fixture-daemon"
            s["stopped_bank_selectors"] = kw
        @contextmanager
        def helper(self, script):
            s["calls"].append(["docker", "create", "--network", "none", "--volumes-from", "fixture-daemon:ro", s["running"]["Image"], script])
            try:
                yield "owned-helper"
            finally:
                s["calls"].append(["docker", "rm", "-f", "owned-helper"])
        def command(self, *args, **kw):
            s["calls"].append(["docker", *args])
            return (1 if s["fail"] == "probe" else 0), ("58" if engine == "rust" else "SCHEMA_META_VERSION = 58\n")
        def backup(self, *, out, keep_days):
            assert keep_days is None
            s["calls"].append(["complete-stopped-backup"])
            if s["fail"] == "backup":
                raise up.UpdateError("fixture backup failed")
            out.mkdir(parents=True, exist_ok=True)
            dump, state = out / "fixture.sql.gz", out / "fixture.tgz"
            dump.write_bytes(gzip.compress(b"-- PostgreSQL database dump complete\n"))
            with tarfile.open(state, "w:gz") as archive:
                member = tarfile.TarInfo("config.yaml")
                member.size = 2
                archive.addfile(member, io.BytesIO(b"{}"))
            return {"dump": dump, "state": state}

    monkeypatch.setattr(up, "run_cli", run)
    monkeypatch.setitem(sys.modules, "pseudolife_memory.docker_backup_restore", SimpleNamespace(DockerBank=StoppedBank))
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "fixture-stored" if stored else "fixture-host-must-not-win")
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN_FILE", raising=False)
    update = s["update"](None, allow_auth_change=True, daemon_only=True, env_file=env)
    return update, override, shadow


@pytest.mark.parametrize("engine,stored", [("python", False), ("rust", False), ("python", True), ("rust", True)])
def test_stopped_checkout_handover_backs_up_exact_image_and_retains_selection(engine_host, monkeypatch, engine, stored):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    update, override, shadow = _stopped_handover(s, monkeypatch, engine=engine, stored=stored)
    checked = []
    monkeypatch.setattr(eng, "authenticated_read", lambda url, token: checked.append(token == ("fixture-stored" if stored else "fixture-carried")) or True)
    repo = eng.selected_checkout(update)
    assert repo == s["repo"]
    eng.deploy_engine(update, repo)
    calls = s["calls"]
    backup = next(i for i, call in enumerate(calls) if call == ["complete-stopped-backup"])
    tag = next(i for i, call in enumerate(calls) if call[1:2] == ["tag"])
    build = next(i for i, call in enumerate(calls) if "build" in call)
    assert backup < tag < build
    assert calls[tag][2] == "sha256:validated-python"
    assert checked == [True]  # Only the mandatory post-recreate authenticated gate.
    assert override.read_text() == "# retained custom host configuration\n"
    assert shadow.read_text() == "fixture-shadow\n"
    assert all("ghcr.io" not in " ".join(call) for call in calls)
    assert not (s["repo"] / "data/update-engine/docker-compose.engine-candidate.json").exists()
    selected = eng._selection(s["repo"], eng.SELECTION if engine == "rust" else eng.PYTHON_ACCEPTED)
    assert selected["labels"][eng.ENGINE_LABEL] == engine
    assert any(call[1:2] == ["rm"] and call[-1] == "owned-helper" for call in calls)
    assert not any(call[1:3] == ["exec", "fixture-daemon"] for call in calls)


@pytest.mark.parametrize("engine", ["python", "rust"])
def test_running_checkout_refuses_even_a_subset_bearer_handover(engine_host, monkeypatch, engine):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    update, override, _ = _stopped_handover(s, monkeypatch, engine=engine)
    update._selected_engine = engine
    s["running"]["State"]["Running"] = True
    s["running"]["Config"]["Env"] = [
        "PSEUDOLIFE_MCP_TOKEN=fixture-carried",
        "PSEUDOLIFE_MCP_TOKENS=fixture-carried:owner,fixture-removed:peer",
        "PSEUDOLIFE_MCP_DATABASE_URL=postgresql://fixture:fixture@pseudolife-pg:5432/fixture"]
    s["config"]["services"][up.DAEMON_SERVICE]["environment"]["PSEUDOLIFE_MCP_TOKENS"] = "fixture-carried:owner"
    before = (s["repo"] / "ops/.env").read_bytes(), override.read_bytes()
    with pytest.raises(up.UpdateError, match="stopped"):
        eng.deploy_engine(update, s["repo"])
    assert ((s["repo"] / "ops/.env").read_bytes(), override.read_bytes()) == before
    assert not any(call[1:2] == ["tag"] or "build" in call or "up" in call or "backup.ps1" in " ".join(call)
                   or "backup.sh" in " ".join(call) or call == ["complete-stopped-backup"] for call in s["calls"])


def test_stopped_checkout_handover_uses_runtime_bank_not_cluster_initialization(engine_host, monkeypatch):
    from psycopg.conninfo import conninfo_to_dict
    from pseudolife_memory import update_engine as eng
    s = engine_host
    update, _, _ = _stopped_handover(s, monkeypatch)
    uri = "postgresql://actual_role:fixture@pseudolife-pg:5432/actual_bank"
    s["running"]["Config"]["Env"][-1] = "PSEUDOLIFE_MCP_DATABASE_URL=" + uri
    s["config"]["services"][up.DAEMON_SERVICE]["environment"]["PSEUDOLIFE_MCP_DATABASE_URL"] = uri
    update._selected_engine = "python"
    eng.deploy_engine(update, s["repo"])
    queries = [call for call in s["calls"] if "psql" in call]
    assert queries and all(call[call.index("-U") + 1] == "actual_role" for call in queries)
    assert all(conninfo_to_dict(call[call.index("-d") + 1]) == {"dbname": "actual_bank"} for call in queries)
    assert all(call[call.index("-h") + 1] == "/var/run/postgresql" for call in queries)


@pytest.mark.parametrize("port", [5432, 6543])
def test_stopped_handover_retains_runtime_port_and_literal_database(engine_host, monkeypatch, port):
    from psycopg.conninfo import make_conninfo, conninfo_to_dict
    from pseudolife_memory import update_engine as eng
    s = engine_host
    update, _, _ = _stopped_handover(s, monkeypatch)
    database = "host=elsewhere bank'quote"
    source = make_conninfo(host="pseudolife-pg", port=port, user="actual_role", dbname=database)
    s["running"]["Config"]["Env"][-1] = "PSEUDOLIFE_MCP_DATABASE_URL=" + source
    s["config"]["services"][up.DAEMON_SERVICE]["environment"]["PSEUDOLIFE_MCP_DATABASE_URL"] = source
    update._selected_engine = "python"
    eng.deploy_engine(update, s["repo"])
    queries = [call for call in s["calls"] if "psql" in call]
    assert queries and all(call[call.index("-p") + 1] == str(port) for call in queries)
    assert all(conninfo_to_dict(call[call.index("-d") + 1]) == {"dbname": database} for call in queries)
    assert s["stopped_bank_selectors"]["db_port"] == port
    assert s["stopped_bank_selectors"]["db"] == database


def test_stopped_checkout_handover_refuses_aliases_on_unshared_networks(engine_host, monkeypatch):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    update, _, _ = _stopped_handover(s, monkeypatch)
    s["running"]["NetworkSettings"] = {"Networks": {"unrelated": {}}}
    update._selected_engine = "python"
    with pytest.raises(up.UpdateError):
        eng.deploy_engine(update, s["repo"])
    assert not any(call[1:2] == ["tag"] or "build" in call or "up" in call for call in s["calls"])


@pytest.mark.parametrize("failure", ["backup", "probe"])
def test_stopped_checkout_handover_failure_precedes_tag_build(engine_host, monkeypatch, failure):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    update, _, _ = _stopped_handover(s, monkeypatch)
    s["fail"] = failure
    update._selected_engine = "python"
    with pytest.raises(up.UpdateError):
        eng.deploy_engine(update, s["repo"])
    assert not any(call[1:2] == ["tag"] or "build" in call or "up" in call for call in s["calls"])
    assert any(call[1:2] == ["rm"] for call in s["calls"])


def test_stopped_checkout_without_authorization_still_refuses(engine_host):
    from pseudolife_memory import update_engine as eng
    s = engine_host
    s["running"]["State"]["Running"] = False
    with pytest.raises(up.UpdateError, match="must be running"):
        eng.deploy_engine(s["update"](), s["repo"])
    assert not any(call[1:2] == ["tag"] or "build" in call or "up" in call for call in s["calls"])

@pytest.mark.parametrize("failure", ["database", "image", "schema", "stored-bearer", "dependency", "invalid-map", "post-read"])
def test_stopped_checkout_handover_requires_every_precondition(engine_host, monkeypatch, failure):
    import sys
    from pseudolife_memory import update_engine as eng
    s = engine_host
    update, _, _ = _stopped_handover(s, monkeypatch, stored=failure == "stored-bearer")
    update._selected_engine = "python"
    if failure == "database":
        s["config"]["services"][up.DAEMON_SERVICE]["environment"]["PSEUDOLIFE_MCP_DATABASE_URL"] = "postgresql://fixture:fixture@external-pg:5432/fixture"
        s["running"]["Config"]["Env"][-1] = "PSEUDOLIFE_MCP_DATABASE_URL=postgresql://fixture:fixture@external-pg:5432/fixture"
    elif failure == "image":
        old = up.run_cli
        monkeypatch.setattr(up, "run_cli", lambda argv, **kw: (1, "") if argv[1:3] == ["image", "inspect"] else old(argv, **kw))
    elif failure == "schema":
        s["schema"] = "59"
    elif failure == "stored-bearer":
        monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "fixture-unrelated")
    elif failure == "dependency":
        monkeypatch.setitem(sys.modules, "pseudolife_memory.docker_backup_restore", None)
    elif failure == "invalid-map":
        s["config"]["services"][up.DAEMON_SERVICE]["environment"]["PSEUDOLIFE_MCP_TOKENS"] = "malformed"
    else:
        monkeypatch.setattr(eng, "authenticated_read", lambda *args: False)
    with pytest.raises(up.UpdateError):
        eng.deploy_engine(update, s["repo"])
    if failure != "post-read":
        assert not any(call[1:2] == ["tag"] or "build" in call or "up" in call for call in s["calls"])
    else:
        assert update.report.rollback["state"] == "tagged"
        assert not (s["repo"] / eng.PYTHON_ACCEPTED).exists()
        assert not (s["repo"] / "data/update-engine/docker-compose.engine-candidate.json").exists()
