"""Phase 2 CLI admission and byte comparison controls."""
import ast
import base64
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from evals.rust_port import cli_corpus, cli_dispatch, harness, pytest_plugin


def test_cli_environment_selector_and_explicit_override(monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_PORT_CLI_JSON", '["environment-candidate"]')
    config = SimpleNamespace(getoption=lambda name: None)
    pytest_plugin.pytest_configure(config)
    assert config._port_cli_prefix == ["environment-candidate"]
    config = SimpleNamespace(getoption=lambda name: '["explicit-candidate"]'
                             if name == "--port-cli-json" else None)
    pytest_plugin.pytest_configure(config)
    assert config._port_cli_prefix == ["explicit-candidate"]


def test_public_cli_subprocess_adapter_routes_only_public_entrypoints():
    import sys
    assert pytest_plugin.public_cli_arguments([sys.executable, "-m", "pseudolife_memory.cli", "doctor"]) == ["doctor"]
    assert pytest_plugin.public_cli_arguments(["pseudolife-mcp", "lease", "list"]) == ["lease", "list"]
    assert pytest_plugin.public_cli_arguments([sys.executable, "-m", "pseudolife_memory.doctor_cli"]) is None
    assert pytest_plugin.boundary("tests/test_shim.py::test_doctor_checks_registered_runtime_handshake_without_bank_writes") is None
    assert pytest_plugin.boundary("tests/test_cli_dispatch.py::test_version_from_a_runtime_names_its_directory_and_commit") == "cli-main-process"


@pytest.mark.parametrize("drift", [None, "hook", "runner", "launcher", "pythonpath"])
def test_doorbell_hook_adapter_refuses_fixture_drift(tmp_path, drift):
    import os
    import sys
    root = tmp_path / "source"
    runner, marker = tmp_path / "consume.py", tmp_path / "helper-launched"
    runner.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('yes')\n"
                      "from pseudolife_memory.cli import main\nmain()\n", encoding="utf-8")
    launcher = tmp_path / "consume.sh"
    launcher.write_text(f'#!/usr/bin/env bash\nexec "{Path(sys.executable).as_posix()}" '
                        f'"{runner.as_posix()}" "$@"\n', encoding="utf-8")
    env = dict(PSEUDOLIFE_SHIM_LAUNCHER=launcher.as_posix(), PYTHONPATH=str(root))
    command = ["bash", str(root / "plugin/hooks/coordination-prompt.sh")]
    if drift == "hook":
        command.append("--changed")
    elif drift in {"runner", "launcher"}:
        (runner if drift == "runner" else launcher).write_text("changed\n", encoding="utf-8")
    elif drift == "pythonpath":
        env["PYTHONPATH"] += os.pathsep + "ambient-source"
    if drift:
        with pytest.raises(pytest.UsageError, match="changed"):
            pytest_plugin.doorbell_hook_environment(command, env, root, tmp_path)
    else:
        assert pytest_plugin.doorbell_hook_environment(command, env, root, tmp_path) == runner
    assert not marker.exists()  # Validation must not fabricate the helper's marker.


def test_doorbell_child_adapter_rejects_mode_drift_before_candidate(tmp_path):
    import json
    import os
    import sys
    source = tmp_path / "source"
    package = source / "pseudolife_memory"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text("def main(): raise RuntimeError('oracle fallback')\n")
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / "sitecustomize.py").write_text(pytest_plugin.doorbell_child_source(), encoding="utf-8")
    runner, marker = tmp_path / "consume.py", tmp_path / "helper-launched"
    runner.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('yes')\n"
                      "from pseudolife_memory.cli import main\nmain()\n", encoding="utf-8")
    candidate = tmp_path / "candidate.py"
    launched = tmp_path / "candidate-launched"
    candidate.write_text(f"from pathlib import Path\nPath({str(launched)!r}).write_text('yes')\n")
    witness, config = tmp_path / "witness", adapter / "config.json"
    config.write_text(json.dumps(dict(runner=str(runner), cli_source=str(package / "cli.py"),
                                     prefix=[sys.executable, str(candidate)], timeout=10,
                                     witness=str(witness))))
    result = subprocess.run([sys.executable, str(runner), "doctor"],
                            env=dict(os.environ, PYTHONPATH=os.pathsep.join(map(str, (adapter, source))),
                                     PSEUDOLIFE_PORT_HOOK_CONFIG=str(config)),
                            capture_output=True, timeout=10)
    assert result.returncode != 0 and b"changed public mode" in result.stderr
    assert marker.read_text() == "yes" and not launched.exists() and not witness.exists()


def test_doorbell_adapter_preserves_missing_shell_skip(monkeypatch):
    module = SimpleNamespace(shutil=SimpleNamespace(which=lambda name: None))
    request = SimpleNamespace(node=SimpleNamespace(module=module,
                                                   callspec=SimpleNamespace(params={"platform_hook": "windows"})))
    pytest_plugin._route_doorbell_hook(request, monkeypatch, ["candidate"])
    module.bash_exe = lambda: pytest.skip("Bash is not installed")
    request.node.callspec.params["platform_hook"] = "posix"
    with pytest.raises(pytest.skip.Exception, match="Bash is not installed"):
        pytest_plugin._route_doorbell_hook(request, monkeypatch, ["candidate"])


def test_cli_dispatch_includes_exact_admitted_hooks_without_duplicates(monkeypatch):
    historical = {node for node, mode in pytest_plugin.MANIFEST["mapped"].items()
                  if mode == "cli-main-process"}
    admitted = pytest_plugin.CLI_HOOK_NODES
    nodes = cli_dispatch.selected_nodes()
    assert len(historical) == 5 and len(admitted) == 4 and not pytest_plugin.CLI_SUBPROCESS_NODES
    assert nodes == sorted(historical | admitted) and len(nodes) == 9
    monkeypatch.setattr(pytest_plugin, "CLI_HOOK_NODES", admitted | historical)
    assert cli_dispatch.selected_nodes() == nodes
    monkeypatch.setattr(pytest_plugin, "CLI_HOOK_NODES", set())
    assert cli_dispatch.selected_nodes() == sorted(historical)


@pytest.mark.parametrize("drift", [None, "package", "distribution", "origin", "python"])
@pytest.mark.parametrize("version", ["0.17.0", "0.17.1+pin"])
def test_cli_dispatch_runtime_requires_the_selected_pin(tmp_path, monkeypatch, drift, version):
    (tmp_path / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n', encoding="utf-8")
    runtime = dict(source_origin_matches_selected_root=True, package_runtime_version=version,
                   distribution_versions={"pseudolife-mcp": version})
    if drift == "package":
        runtime["package_runtime_version"] = "0.16.1"
    elif drift == "distribution":
        runtime["distribution_versions"]["pseudolife-mcp"] = "0.16.1"
    elif drift == "origin":
        runtime["source_origin_matches_selected_root"] = False
    elif drift == "python":
        monkeypatch.setattr(cli_dispatch.sys, "version_info", (3, 12, 0))
    monkeypatch.setattr(cli_dispatch, "runtime_metadata", lambda root: runtime)
    if drift:
        with pytest.raises(RuntimeError, match="genuine pinned installed package runtime"):
            cli_dispatch.require_cli_runtime(tmp_path)
    else:
        assert cli_dispatch.require_cli_runtime(tmp_path) is runtime


def test_global_cli_selector_refuses_deferred_doctor(monkeypatch):
    doctor = "tests/test_shim.py::test_doctor_checks_registered_runtime_handshake_without_bank_writes"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not dispatch doctor"))
    config = SimpleNamespace(_port_cli_prefix=["candidate"], getoption=lambda name: False)
    with pytest.raises(pytest.UsageError, match="no process adapter"):
        pytest_plugin.pytest_collection_finish(SimpleNamespace(config=config, items=[SimpleNamespace(nodeid=doctor)]))
    request = SimpleNamespace(config=config, node=SimpleNamespace(nodeid=doctor))
    with pytest.raises(pytest.UsageError, match="not implemented"):
        pytest_plugin._port_selected_boundary.__wrapped__(request)


def test_stdio_full_suite_leaves_doctor_in_python(monkeypatch):
    doctor = "tests/test_shim.py::test_doctor_checks_registered_runtime_handshake_without_bank_writes"
    stdio = next(node for node, mode in pytest_plugin.MANIFEST["mapped"].items() if mode == "stdio-shim-process")
    config = SimpleNamespace(_port_stdio_prefix=["candidate"], getoption=lambda name: True,
                             pluginmanager=SimpleNamespace(getplugin=lambda name: None))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not dispatch doctor"))
    pytest_plugin.pytest_collection_finish(SimpleNamespace(config=config, items=[
        SimpleNamespace(nodeid=doctor), SimpleNamespace(nodeid=stdio)]))
    pytest_plugin._port_selected_boundary.__wrapped__(SimpleNamespace(config=config, node=SimpleNamespace(nodeid=doctor)))


@pytest.mark.parametrize("value", ["invalid", "[]", '[""]', '[3]', '{}'])
def test_cli_environment_selector_fails_closed(monkeypatch, value):
    monkeypatch.setenv("PSEUDOLIFE_PORT_CLI_JSON", value)
    with pytest.raises(pytest.UsageError):
        pytest_plugin.pytest_configure(SimpleNamespace(getoption=lambda name: None))


def test_cli_corpus_is_additive_and_does_not_invent_outputs():
    cases = cli_corpus.corpus()["cases"]
    assert len(cases) == len({case["id"] for case in cases}) == 15
    assert all("response" not in case for case in cases)
    assert all(case["policy"] == {"source_text_paths": [], "ignored_values": []}
               for case in cases)
    assert {tuple(case["request"]["argv"]) for case in cases} >= {
        ("--help",), ("-h",), ("help",), ("help", "anything"), ("bogus",)}
    contract = cli_corpus.corpus()["fixture"]["stream_contract"]
    assert contract["stdout_stderr_encoding"] == "utf-8"
    assert contract["argv_domain"] == "valid Unicode scalar strings"
    assert contract["newlines"] == "Windows CRLF; LF on other platforms"
    assert "locale/default and other output encodings" in contract["deferred"]


@pytest.mark.parametrize("field,replacement", [
    ("exit_code", 2), ("stdout_b64", base64.b64encode(b"line\r\n").decode()),
    ("stderr_b64", base64.b64encode(b"unexpected\n").decode()),
])
def test_cli_judge_rejects_exit_stream_and_newline_changes(field, replacement):
    expected = {"exit_code": 0, "stdout_b64": base64.b64encode(b"line\n").decode(),
                "stderr_b64": ""}
    actual = {**expected, field: replacement}
    assert harness.compare(expected, actual,
                           harness.Policy(source_text_paths=(), ignored_values=()))


def pinned_usage():
    root = Path(__file__).resolve().parents[2]
    source = subprocess.check_output([
        "git", "show", "f709abb54f7912ae9cd767998d0926ca33df4bcd:pseudolife_memory/cli.py"],
        cwd=root)
    tree = ast.parse(source.decode("utf-8"))
    return next(ast.literal_eval(node.value) for node in tree.body
                if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "_USAGE"
                    for target in node.targets)).encode("utf-8")


def test_help_source_matches_pinned_python_literal_bytes():
    root = Path(__file__).resolve().parents[2]
    assert (root / "rust/shim/src/cli_help.txt").read_bytes() == pinned_usage()


def test_help_asset_stays_canonical_in_autocrlf_checkout(tmp_path):
    root = Path(__file__).resolve().parents[2]
    source = tmp_path / "source"
    asset = source / "rust/shim/src/cli_help.txt"
    asset.parent.mkdir(parents=True)
    asset.write_bytes((root / "rust/shim/src/cli_help.txt").read_bytes())
    attributes = root / "rust/.gitattributes"
    if attributes.exists():
        (source / "rust/.gitattributes").write_bytes(attributes.read_bytes())
    subprocess.run(["git", "init", "--quiet", str(source)], check=True)
    subprocess.run(["git", "-c", "core.autocrlf=true", "add", "rust"],
                   cwd=source, check=True)
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "-c", "core.autocrlf=true", "checkout-index",
                    f"--prefix={checkout.as_posix()}/", "--", "rust/shim/src/cli_help.txt"],
                   cwd=source, check=True)
    assert (checkout / "rust/shim/src/cli_help.txt").read_bytes() == pinned_usage()
