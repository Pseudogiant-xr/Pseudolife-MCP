"""Installer-schema corpus admission and identical minimal runtime preparation."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.rust_port import cli_process, cli_version


def test_public_version_corpus_contains_installed_default_override_and_bare_cases():
    rows = cli_process.cases(("help", "version"))
    versions = [row for row in rows if row["mode"] == "version"]
    assert len(rows) == 26
    assert len(versions) == 11
    assert {row["layout_kind"] for row in versions} == {
        "bare", "default", "override", "half-root", "half-launcher", "wrong-suffix"}
    assert {row["manifest_kind"] for row in versions} == {"commit", "requirement", "checkout-no-git"}
    assert all(not row["normalizations"] for row in versions)


def test_hosted_process_gate_explicitly_includes_version():
    workflow = Path(__file__).resolve().parents[2] / ".github/workflows/rust.yml"
    invocation = next(line for line in workflow.read_text().splitlines()
                      if "& $oraclePython -c $processBootstrap" in line)
    assert "--modes help version" in invocation


@pytest.mark.parametrize("platform", ["nt", "posix"])
@pytest.mark.parametrize("spec", cli_version.cases(), ids=lambda row: row["id"])
def test_minimal_runtime_seeds_identical_bytes_and_preserves_public_prefix(tmp_path, monkeypatch, platform, spec):
    seed = tmp_path / "seed"
    seed.mkdir()
    files = {name: seed / name for name in ("python", "console", "pyvenv.cfg", "native")}
    for name, file in files.items():
        file.write_bytes(("actual fixture " + name).encode())
    commands = {"oracle": [str(files["python"]), "-m", "pseudolife_memory.cli"],
                "candidate": [str(files["native"])]}
    monkeypatch.setattr(cli_version, "seed_context", lambda root: {
        "python": files["python"], "console": files["console"], "config": files["pyvenv.cfg"],
        "base_interpreter": str(files["python"]), "pythonpath": str(seed), "version": "0.16.1"})
    # Simulate each naming/layout branch without replacing pathlib's host OS.
    monkeypatch.setattr(cli_version, "os", SimpleNamespace(name=platform))
    prepare = cli_version.make_prepare(tmp_path, "f" * 40)
    monkeypatch.setattr(cli_process, "run_cli", lambda *a, **k: {
        "exit_code": 0, "stdout_b64": "", "stderr_b64": ""})
    home = tmp_path / "home"
    arms = {arm: cli_process.observe(spec, prefix, commands, root=tmp_path, home=home,
                                    url="http://127.0.0.1:49152", prepare=prepare)
            for arm, prefix in commands.items()}
    assert cli_process.byte_payload(arms["oracle"]) == cli_process.byte_payload(arms["candidate"])
    assert len(arms["oracle"]["pre_files_b64"]) <= 11  # Token plus two minimal runtimes, never dependencies.
    for arm in commands:
        execution = arms[arm]["execution"]
        assert execution["selected_prefix"][1:] == commands[arm][1:]
        assert execution["command_identity"]["executable_sha256"] == \
            cli_process.command_identity(commands[arm], tmp_path)["executable_sha256"]
    native = Path(arms["candidate"]["execution"]["selected_prefix"][0])
    if spec["layout_kind"] == "bare":
        assert native.parent == home / "bare-native"
    else:
        root = home / "override" if spec["layout_kind"] == "override" else \
            home / ("local" if platform == "nt" else "data") / "pseudolife-mcp"
        assert native.parent == root / "runtimes" / "000001"
    import base64
    import json
    markers = {path: json.loads(base64.b64decode(value))
               for path, value in arms["oracle"]["pre_files_b64"].items() if path.endswith("runtime.json")}
    assert all(set(marker) == {"version", "installed_at", "source", "source_commit", "base_interpreter"}
               for marker in markers.values())
    assert all(marker["version"] == "0.16.1" for marker in markers.values())
    if spec["manifest_kind"] != "commit":
        assert next(marker for path, marker in markers.items()
                    if path.endswith("/000001/runtime.json"))["source_commit"] is None


def test_seed_context_uses_actual_venv_files_and_observed_dependency_locations(tmp_path, monkeypatch):
    import os
    runtime = tmp_path / "venv"
    scripts = runtime / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir(parents=True)
    console = scripts / ("pseudolife-mcp.exe" if os.name == "nt" else "pseudolife-mcp")
    console.write_bytes(b"installed console")
    (runtime / "pyvenv.cfg").write_text("home = genuine base\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion="0.16.1"\n')
    dependency = tmp_path / "dependency" / "site-packages"
    dependency.mkdir(parents=True)
    monkeypatch.setattr(cli_version.sys, "prefix", str(runtime))
    monkeypatch.setattr(cli_version.sys, "base_prefix", str(tmp_path / "base"))
    monkeypatch.setattr(cli_version.sys, "path", [str(tmp_path), str(dependency), str(dependency)])
    observed = cli_version.seed_context(tmp_path)
    assert observed["config"] == runtime / "pyvenv.cfg"
    assert observed["console"] == console
    assert observed["pythonpath"] == os.pathsep.join((str(tmp_path), str(dependency.resolve())))


def test_corpus_run_automatically_selects_installer_preparer_before_owned_launches(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from evals.rust_baseline import daemon
    (tmp_path / "pyproject.toml").write_text('[project]\nversion="0.16.1"\n')
    candidate = tmp_path / "native"
    candidate.write_bytes(b"native")
    monkeypatch.setattr(cli_process, "require_phase1_source", lambda root: {"oracle_head": "f" * 40})
    monkeypatch.setattr(cli_process, "require_import_root", lambda root: None)
    monkeypatch.setattr(cli_process, "runtime_metadata", lambda root: {
        "source_origin_matches_selected_root": True, "package_runtime_version": "0.16.1",
        "distribution_versions": {"pseudolife-mcp": "0.16.1"}})
    monkeypatch.setattr(cli_process, "candidate_identity", cli_process.command_identity)
    monkeypatch.setattr(cli_process, "cli_binding", lambda *args, **kwargs: {})
    callback = lambda *arguments: arguments[3]
    selected = []
    monkeypatch.setattr(cli_version, "make_prepare", lambda root, pin: selected.append((root, pin)) or callback)

    @contextmanager
    def database():
        yield "fixture database"

    @contextmanager
    def launched(*args, **kwargs):
        assert selected == [(tmp_path, "f" * 40)]
        yield None, "http://127.0.0.1:49152", {}

    @contextmanager
    def directory():
        yield tmp_path

    monkeypatch.setattr(daemon, "disposable_database", database)
    monkeypatch.setattr(daemon, "launched_daemon", launched)
    monkeypatch.setattr(daemon, "private_directory", directory)

    def paired(*args, **kwargs):
        assert kwargs["prepare"] is callback
        return {"passed": True, "records": [], "candidate_output_controls": []}

    monkeypatch.setattr(cli_process, "paired_cases", paired)
    assert cli_process.run(tmp_path, [str(candidate)], tmp_path, cli_version.cases(), {})["passed"]
