"""Pure transport and config invariants; no installed daemon or database."""
from __future__ import annotations

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
def test_native_image_harness_seam_executes_the_real_oracle(tmp_path, mode, code):
    import sys
    from pathlib import Path
    from cli_harness import core
    from cli_harness.rows.maintainer_setup import cases

    case = next(c for c in cases() if c.id == "setup-rust-image-" + mode)
    source = Path(__file__).resolve().parents[2]
    result = core.run_arm(case, core.python_target(sys.executable, source), tmp_path / "home")
    assert result["exit"] == code
