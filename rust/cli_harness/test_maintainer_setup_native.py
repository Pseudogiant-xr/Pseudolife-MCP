"""Pure transport and config invariants; no installed daemon or database."""
from __future__ import annotations

import base64
import json
import subprocess

import pytest


@pytest.mark.parametrize("args,tail", [
    (("state",), ["maintainer", "setup", "--host-state"]),
    (("write", "localhost", "http://localhost:8765"),
     ["maintainer", "setup", "--host-write", "localhost", "http://localhost:8765"]),
])
def test_rust_image_host_helpers_use_the_installed_native_cli(monkeypatch, args, tail):
    from pseudolife_memory import maintainer_setup as setup

    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "inspect":
            return subprocess.CompletedProcess(argv, 0, "rust\n", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"fixture": True}), "")

    monkeypatch.setattr(setup, "run", run)
    monkeypatch.setattr(setup, "docker_cmd", lambda: "fixture-docker")
    assert setup._host("docker", *args) == {"fixture": True}
    assert calls[-1] == ["fixture-docker", "exec", setup.DAEMON_CONTAINER,
                         "pseudolife-mcp", *tail]


def test_rust_image_enrolment_keeps_the_same_native_environment(monkeypatch):
    from pseudolife_memory import maintainer_setup as setup

    monkeypatch.setattr(setup, "docker_cmd", lambda: "fixture-docker")
    monkeypatch.setattr(setup, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, "rust\n", ""))
    assert setup._maintainer("docker", "enrol-code") == [
        "fixture-docker", "exec", setup.DAEMON_CONTAINER, "pseudolife-mcp",
        "maintainer", "enrol-code"]


def test_python_image_retains_the_oracle_helper_command(monkeypatch):
    from pseudolife_memory import maintainer_setup as setup

    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "inspect":
            return subprocess.CompletedProcess(argv, 0, "python\n", "")
        return subprocess.CompletedProcess(argv, 0, "{}\n", "")

    monkeypatch.setattr(setup, "run", run)
    monkeypatch.setattr(setup, "docker_cmd", lambda: "fixture-docker")
    assert setup._host("docker", "state") == {}
    assert calls[-1] == ["fixture-docker", "exec", setup.DAEMON_CONTAINER, "python",
                         "-m", setup.MODULE, "state"]


def test_write_keeps_unrelated_values_and_original_backup(tmp_path, monkeypatch):
    import yaml
    from pseudolife_memory import maintainer_setup as setup

    path = tmp_path / "config.yaml"
    original = ("memory:\n  preset: flat\n  nested: {flag: yes, octal: 017, label: 'on'}\n"
                "coordination:\n  enabled: false\n  maintainer:\n    maintainer_per_recipient_per_hour: 7\n")
    path.write_text(original, encoding="utf-8")
    monkeypatch.setenv("PSEUDOLIFE_MCP_CONFIG", str(path))
    result = setup._write("localhost", "http://localhost:8765")
    from pathlib import Path
    assert Path(result["backup"]).read_text(encoding="utf-8") == original
    after = yaml.safe_load(path.read_text(encoding="utf-8"))
    before = yaml.safe_load(original)
    before["coordination"]["maintainer"].update(rp_id="localhost", origin="http://localhost:8765")
    assert after == before


def test_restart_failure_does_not_discard_the_saved_name(monkeypatch):
    from pseudolife_memory import maintainer_setup as setup

    monkeypatch.setattr(setup, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 1, "", "fixture restart failure"))
    with pytest.raises(setup._Refused) as caught:
        setup._restart("docker", 8765)
    assert caught.value.code == 5
    assert "the config is written" in str(caught.value)
    assert "maintainer setup" in str(caught.value)


@pytest.mark.parametrize("mode,code", [("active", 0), ("pending", 1), ("stopped", 4),
                                      ("fresh", 0), ("restart-fails", 5), ("restart-retry", 0)])
@pytest.mark.parametrize("engine", ["rust", "python", "unlabelled"])
def test_native_image_harness_seam_executes_the_real_oracle(tmp_path, mode, code, engine):
    import sys
    from pathlib import Path
    from cli_harness import core
    from cli_harness.rows.maintainer_setup import cases

    case = next(c for c in cases() if c.id == "setup-" + engine + "-image-" + mode)
    source = Path(__file__).resolve().parents[2]
    result = core.run_arm(case, core.python_target(sys.executable, source), tmp_path / "home")
    assert result["exit"] == code


@pytest.mark.parametrize("cap", [-1, True, 1.5, "7", None])
def test_setup_rejects_invalid_retained_rate_limit_before_backup(tmp_path, monkeypatch, cap):
    import yaml
    from pseudolife_memory import maintainer_setup as setup

    path = tmp_path / "config.yaml"
    original = yaml.safe_dump({"coordination": {"maintainer": {
        "rp_id": "localhost", "origin": "http://localhost:8765",
        "maintainer_per_recipient_per_hour": cap}}})
    path.write_text(original, encoding="utf-8")
    monkeypatch.setenv("PSEUDOLIFE_MCP_CONFIG", str(path))
    monkeypatch.setattr(setup, "_bank_keys", lambda: ([], None))
    expected = ("coordination.maintainer.maintainer_per_recipient_per_hour must "
                "be a whole number, 0 or more")
    assert setup._state()["problem"] == expected
    with pytest.raises(ValueError, match="maintainer_per_recipient_per_hour"):
        setup._write("localhost", "http://localhost:8765")
    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("kind", ["negative", "boolean", "fraction", "string", "null"])
def test_invalid_rate_limit_harness_case_uses_the_real_oracle(tmp_path, kind):
    import sys
    from pathlib import Path
    from cli_harness import core
    from cli_harness.rows.maintainer_setup import cases

    case = next(c for c in cases() if c.id == "setup-invalid-cap-" + kind)
    source = Path(__file__).resolve().parents[2]
    result = core.run_arm(case, core.python_target(sys.executable, source), tmp_path / "home")
    assert result["exit"] == 5
    assert b"writing the daemon's config failed: refused:" in base64.b64decode(result["stderr"])


def _native():
    import os
    candidate = os.environ.get("R1_PAIR_CANDIDATE") or os.environ.get("PSEUDOLIFE_RUST_BINARY")
    if not candidate:
        pytest.skip("R1_PAIR_CANDIDATE is required for the native arm")
    return candidate


def _native_env(tmp_path):
    import os
    env = {k: v for k, v in os.environ.items() if not k.startswith("PSEUDOLIFE_")}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), LOCALAPPDATA=str(tmp_path),
               XDG_DATA_HOME=str(tmp_path), PATH=str(tmp_path / "empty-bin"),
               PSEUDOLIFE_MCP_CONFIG=str(tmp_path / "config.yaml"),
               PSEUDOLIFE_MCP_DATA_DIR=str(tmp_path / "data"))
    return env


@pytest.mark.parametrize("text", [
    "other: !!timestamp 2026-01-02\n",
    "other: 2026-1-2T01:02:03Z\n",
    "other: !!binary SGVsbG8=\n",
    "other: !!set {one: null, two: null}\n",
    "other: !!omap [{one: 1}, {two: 2}]\n",
    "other: !!pairs [{one: 1}, {one: 2}]\n",
    "other: !!seq [1, 2]\n",
    "other: !!map {one: 1}\n",
    "other: {17: kept, true: kept}\n",
    "defaults: &defaults {rp_id: ignored.invalid, origin: https://ignored.invalid}\n",
])
def test_native_host_state_reads_safe_yaml_without_rewriting(tmp_path, monkeypatch, text):
    from pseudolife_memory import maintainer_setup as setup
    path = tmp_path / "config.yaml"
    original = text + ("coordination: !!map\n  maintainer: !!map\n"
                       "    rp_id: localhost\n    origin: http://localhost:8765\n")
    path.write_text(original, encoding="utf-8")
    monkeypatch.setenv("PSEUDOLIFE_MCP_CONFIG", str(path))
    monkeypatch.setattr(setup, "_bank_keys", lambda: (None, "NoBank"))
    oracle = setup._state()
    result = subprocess.run([_native(), "maintainer", "setup", "--host-state"],
                            env=_native_env(tmp_path), capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert (state["rp_id"], state["origin"], state["problem"]) == (
        oracle["rp_id"], oracle["origin"], oracle["problem"])
    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("text", [
    "defaults: &base {rp_id: localhost, origin: http://localhost:8765}\n"
    "coordination: {maintainer: {<<: *base}}\n",
    "first: &first {rp_id: localhost, origin: http://localhost:8765}\n"
    "second: &second {rp_id: ignored.invalid, origin: https://ignored.invalid}\n"
    "coordination: {maintainer: {<<: [*first, *second]}}\n",
    "defaults: &base {rp_id: ignored.invalid, origin: https://ignored.invalid}\n"
    "coordination: {maintainer: {rp_id: localhost, <<: *base, origin: http://localhost:8765}}\n",
    "coordination: {maintainer: {'<<': {rp_id: ignored.invalid}, "
    "rp_id: localhost, origin: http://localhost:8765}}\n",
])
def test_native_host_state_resolves_yaml_merge_precedence(tmp_path, monkeypatch, text):
    from pseudolife_memory import maintainer_setup as setup
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("PSEUDOLIFE_MCP_CONFIG", str(path))
    monkeypatch.setattr(setup, "_bank_keys", lambda: (None, "NoBank"))
    oracle = setup._state()
    result = subprocess.run([_native(), "maintainer", "setup", "--host-state"],
                            env=_native_env(tmp_path), capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert (state["rp_id"], state["origin"], state["problem"]) == (
        oracle["rp_id"], oracle["origin"], oracle["problem"])
    assert path.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("special", ["\u2028", "\u2029", *map(chr, range(0x7f, 0xa0))])
def test_native_config_write_keeps_yaml_sensitive_unicode(tmp_path, special):
    import yaml
    path = tmp_path / "config.yaml"
    original = {"other": {"key" + special + "tail": "head" + special + "tail"}}
    path.write_text(yaml.safe_dump(original, allow_unicode=False), encoding="utf-8")
    result = subprocess.run([_native(), "maintainer", "setup", "--host-write",
                             "localhost", "http://localhost:8765"],
                            env=_native_env(tmp_path), capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    after = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert after["other"] == original["other"]


@pytest.mark.parametrize("text", ["2026-1-2", "2026-01-02suffix", "2026-01-02Tnot-a-time"])
def test_native_config_write_keeps_date_like_strings(tmp_path, text):
    import yaml
    path = tmp_path / "config.yaml"
    path.write_text("other: " + text + "\n", encoding="utf-8")
    result = subprocess.run([_native(), "maintainer", "setup", "--host-write",
                             "localhost", "http://localhost:8765"],
                            env=_native_env(tmp_path), capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["other"] == text


@pytest.mark.parametrize("cap", [-1, True, 1.5, "7", None])
def test_native_setup_rejects_invalid_retained_rate_limit_before_backup(tmp_path, cap):
    import yaml
    path = tmp_path / "config.yaml"
    original = yaml.safe_dump({"coordination": {"maintainer": {
        "rp_id": "localhost", "origin": "http://localhost:8765",
        "maintainer_per_recipient_per_hour": cap}}})
    path.write_text(original, encoding="utf-8")
    command = [_native(), "maintainer", "setup"]
    state = subprocess.run(command + ["--host-state"], env=_native_env(tmp_path),
                           capture_output=True, text=True, timeout=20)
    assert state.returncode == 0, state.stderr
    expected = ("coordination.maintainer.maintainer_per_recipient_per_hour must "
                "be a whole number, 0 or more")
    assert json.loads(state.stdout)["problem"] == expected
    written = subprocess.run(command + ["--host-write", "localhost", "http://localhost:8765"],
                             env=_native_env(tmp_path), capture_output=True, text=True, timeout=20)
    assert written.returncode == 2, written.stderr
    assert expected in written.stderr
    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.iterdir()) == [path]


def test_native_setup_refuses_lite_before_config_mutation(tmp_path):
    from .fixture_daemon import FixtureDaemon, json_body
    data = tmp_path / "data" / "embedded_pg"
    data.mkdir(parents=True)
    (data / "PG_VERSION").write_text("17\n", encoding="utf-8")
    path = tmp_path / "config.yaml"
    path.write_text("memory: {preset: flat}\n", encoding="utf-8")
    daemon = FixtureDaemon({"/health": json_body({"auth": True})})
    try:
        result = subprocess.run([_native(), "maintainer", "setup", "--local", "--yes",
                                 "--port", daemon.url.rsplit(":", 1)[1]],
                                env=_native_env(tmp_path), capture_output=True, text=True, timeout=20)
    finally:
        daemon.close()
    assert result.returncode == 4, result.stderr
    assert "phase4-embedded-pg-deferred" in result.stderr
    assert path.read_text(encoding="utf-8") == "memory: {preset: flat}\n"
    assert not list(tmp_path.glob("config.yaml.*"))


def test_native_maintainer_forwarding_preserves_container_exit_status(tmp_path):
    import os
    import shlex
    import sys

    expected = 257 if os.name == "nt" else 7
    script = tmp_path / "docker_fixture.py"
    script.write_text(
        "import sys\na = sys.argv[1:]\n"
        "if a[0] == 'inspect':\n"
        " print('true' if a[2] == '{{.State.Running}}' else 'rust')\n"
        "else:\n"
        " assert a[0] == 'exec' and a[-2:] == ['maintainer', 'list'], a\n"
        f" raise SystemExit({expected})\n", encoding="utf-8")
    docker = tmp_path / ("docker.cmd" if os.name == "nt" else "docker")
    wrapper = (f'@echo off\r\n"{sys.executable}" "{script}" %*\r\nexit /b %errorlevel%\r\n'
               if os.name == "nt" else
               f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n')
    docker.write_text(wrapper, encoding="utf-8")
    docker.chmod(0o700)
    env = _native_env(tmp_path)
    env["PSEUDOLIFE_DOCKER"] = str(docker)
    result = subprocess.run([_native(), "maintainer", "list"], env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == expected, result.stderr
