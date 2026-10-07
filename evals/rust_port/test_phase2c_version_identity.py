"""Additive Phase2c version identity, routing and pinned metadata controls."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.rust_port import cli_dispatch, cli_version, phase1_oracle, pytest_plugin


def test_automatic_cli_selection_includes_the_three_version_admissions():
    nodes = cli_dispatch.selected_nodes()
    assert len(nodes) == len(set(nodes)) == 8
    assert pytest_plugin.CLI_VERSION_NODES <= set(nodes)
    assert all(pytest_plugin.boundary(node) == "cli-main-process" for node in nodes)


@pytest.mark.parametrize("runtime_version,distribution_version", [
    ("0.18.0", "0.18.0"), ("0.16.1", "0.18.0"), ("0.18.0", "0.16.1")])
def test_cli_capture_requires_the_prepared_pin_version(tmp_path, monkeypatch,
                                                      runtime_version, distribution_version):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion="0.18.0"\n')
    monkeypatch.setattr(cli_dispatch, "require_phase1_source", lambda root: {})
    monkeypatch.setattr(cli_dispatch, "require_import_root", lambda root: None)
    monkeypatch.setattr(cli_dispatch, "runtime_metadata", lambda root: {
        "source_origin_matches_selected_root": True,
        "package_runtime_version": runtime_version,
        "distribution_versions": {"pseudolife-mcp": distribution_version}})
    monkeypatch.setattr(cli_dispatch, "sys", SimpleNamespace(version_info=(3, 11), executable="python"))

    def admitted(*args):
        raise RuntimeError("version gate admitted")

    monkeypatch.setattr(cli_dispatch, "candidate_identity", admitted)
    expected = "version gate admitted" if runtime_version == distribution_version == "0.18.0" else "pinned installed"
    with pytest.raises(RuntimeError, match=expected):
        cli_dispatch.run(tmp_path, ["candidate"], tmp_path, tmp_path / "evidence", {})


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_dispatch_text_bridge_rejects_changed_raw_bytes_exit_and_stderr(tmp_path, monkeypatch, platform):
    import base64
    (tmp_path / "pyproject.toml").write_text('[project]\nversion="0.18.0"\n')
    monkeypatch.setattr(cli_version, "os", SimpleNamespace(name=platform))
    text = "pseudolife-mcp 0.18.0\n"
    raw = text.replace("\n", "\r\n").encode() if platform == "nt" else text.encode()
    expected = {"exit_code": 0, "stdout_b64": base64.b64encode(raw).decode(), "stderr_b64": ""}
    assert cli_version.checked_dispatch_text(expected, None, tmp_path) == text
    changes = [text.replace("\n", newline).encode() for newline in ("\r", "\r\r\n", "\n\n", "")]
    changes.append(text.encode() if platform == "nt" else text.replace("\n", "\r\n").encode())
    for changed in changes:
        with pytest.raises(RuntimeError, match="process bytes differ"):
            cli_version.checked_dispatch_text({**expected, "stdout_b64": base64.b64encode(changed).decode()}, None, tmp_path)
    for changed in ({"exit_code": 1}, {"stderr_b64": base64.b64encode(b"unexpected\n").decode()}):
        with pytest.raises(RuntimeError, match="process bytes differ"):
            cli_version.checked_dispatch_text({**expected, **changed}, None, tmp_path)


@pytest.mark.parametrize("platform", ["nt", "posix"])
@pytest.mark.parametrize("arm", ["oracle", "candidate"])
def test_dispatch_runtime_fixture_uses_the_original_runtime_path(tmp_path, monkeypatch, platform, arm):
    import json
    seed = tmp_path / "seed"
    seed.mkdir()
    files = {name: seed / name for name in ("python", "console", "config", "native")}
    for name, path in files.items():
        path.write_bytes(name.encode())
    monkeypatch.setattr(cli_version, "seed_context", lambda root: {
        **files, "base_interpreter": "fixture-base", "pythonpath": str(seed), "version": "0.18.0"})
    monkeypatch.setattr(cli_version, "os", SimpleNamespace(name=platform))
    runtime = SimpleNamespace(path=tmp_path / "runtimes" / "000003", installed_at="",
                              source="/src/checkout", source_commit="ab" * 20)
    command = [cli_version.sys.executable, "-m", "pseudolife_memory.cli"] if arm == "oracle" else [str(files["native"])]
    env = {}
    selected = cli_version.prepare_dispatch_runtime(command, runtime, env, tmp_path)
    scripts = runtime.path / ("Scripts" if platform == "nt" else "bin")
    assert Path(selected[0]).parent == scripts
    assert selected[1:] == command[1:]
    assert Path(selected[0]).read_bytes() == (b"python" if arm == "oracle" else b"native")
    assert env["PSEUDOLIFE_SHIM_RUNTIMES"] == str(runtime.path.parent)
    assert env["PYTHONPATH"] == str(seed)
    assert json.loads((runtime.path / "runtime.json").read_text()) == {
        "version": "0.18.0", "installed_at": "", "source": "/src/checkout",
        "source_commit": "ab" * 20, "base_interpreter": "fixture-base"}


def test_bare_dispatch_version_does_not_construct_an_installed_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_version, "seed_context", lambda root: pytest.fail("bare version has no runtime"))
    command = ["candidate"]
    assert cli_version.prepare_dispatch_runtime(command, None, {}, tmp_path) == command
    assert not list(tmp_path.iterdir())


def test_metadata_fixture_exports_exact_pin_without_installation(tmp_path):
    expected = phase1_oracle.subprocess.check_output(
        ["git", "show", phase1_oracle.ORACLE_HEAD + ":pyproject.toml"], cwd=phase1_oracle.ROOT)
    destination = tmp_path / "metadata"
    result = phase1_oracle.prepare_metadata(destination)
    assert (destination / "pyproject.toml").read_bytes() == expected
    assert list(destination.iterdir()) == [destination / "pyproject.toml"]
    assert result["oracle_head"] == phase1_oracle.ORACLE_HEAD
    with pytest.raises(FileExistsError):
        phase1_oracle.prepare_metadata(destination)
