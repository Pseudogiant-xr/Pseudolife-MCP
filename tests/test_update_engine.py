"""Opt-in engine selection: refusal, persistence, activation and rollback."""
import json
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
