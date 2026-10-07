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


def prompt_subprocess_request(monkeypatch, dispatched, node=None):
    node = node or "tests/test_memory_changes_hook.py::test_prompt_hook_prints_only_changes_and_advances_its_cursor[cli]"
    finalizers = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: dispatched.append((args, kwargs)))
    request = SimpleNamespace(
        config=SimpleNamespace(_port_cli_prefix=["candidate"]),
        node=SimpleNamespace(nodeid=node),
        getfixturevalue=lambda name: monkeypatch,
        addfinalizer=finalizers.append)
    pytest_plugin._port_selected_boundary.__wrapped__(request)
    return finalizers


def test_prompt_subprocess_admission_is_bounded_to_supported_nodes():
    node = "tests/test_memory_changes_hook.py::test_prompt_hook_prints_only_changes_and_advances_its_cursor[cli]"
    admitted = pytest_plugin.CLI_SUBPROCESS_NODES
    help_node = "tests/test_memory_changes_hook.py::test_cli_prompt_hook_is_a_listed_mode"
    assert len(admitted) == 18 and admitted[node] == ["prompt-hook"]
    assert admitted[help_node] == ["--help"]
    assert all(entry.startswith("tests/test_memory_changes_hook.py::") for entry in admitted)
    assert all(argv == (["--help"] if entry == help_node else ["prompt-hook"])
               for entry, argv in admitted.items())
    config = SimpleNamespace(_port_cli_prefix=["candidate"], getoption=lambda name: False)
    pytest_plugin.pytest_collection_finish(SimpleNamespace(config=config, items=[SimpleNamespace(nodeid=node)]))
    for other in (node.replace("[cli]", "[bash]"), node.replace("[cli]", "[native]"),
                  "tests/test_memory_changes_hook.py::test_cli_prompt_hook_imports_neither_the_shim_nor_httpx",
                  "tests/test_memory_changes_hook.py::test_cli_prompt_hook_prints_nothing_through_a_symlinked_cursor"):
        assert pytest_plugin.boundary(other) is None
        with pytest.raises(pytest.UsageError, match="no process adapter"):
            pytest_plugin.pytest_collection_finish(SimpleNamespace(config=config, items=[SimpleNamespace(nodeid=other)]))


@pytest.mark.parametrize("node,argv", [
    ("tests/test_memory_changes_hook.py::test_prompt_hook_prints_only_changes_and_advances_its_cursor[cli]", ["prompt-hook"]),
    ("tests/test_memory_changes_hook.py::test_cli_prompt_hook_is_a_listed_mode", ["--help"]),
])
def test_prompt_subprocess_route_preserves_process_inputs(monkeypatch, node, argv):
    import sys
    dispatched = []
    finalizers = prompt_subprocess_request(monkeypatch, dispatched, node)
    environment = {"PSEUDOLIFE_DIGEST_DIR": "owned", "CUDA_VISIBLE_DEVICES": "0"}
    options = dict(input='{"session_id":"fixture"}', env=environment, cwd="oracle",
                   capture_output=True, text=True, timeout=5, check=True)
    subprocess.run([sys.executable, "-m", "pseudolife_memory.cli", *argv], **options)
    assert dispatched == [((["candidate", *argv],),
                           {**options, "env": {**environment, "CUDA_VISIBLE_DEVICES": "-1"}})]
    assert environment["CUDA_VISIBLE_DEVICES"] == "0"
    finalizers[0]()


@pytest.mark.parametrize("node,argv", [
    (None, ["doctor"]), (None, ["prompt-hook", "--help"]), (None, []),
    ("tests/test_memory_changes_hook.py::test_cli_prompt_hook_is_a_listed_mode", ["prompt-hook"]),
])
def test_prompt_subprocess_route_rejects_argv_drift_before_dispatch(monkeypatch, node, argv):
    import sys
    dispatched = []
    finalizers = prompt_subprocess_request(monkeypatch, dispatched, node)
    with pytest.raises(pytest.UsageError, match="unsupported argv"):
        subprocess.run([sys.executable, "-m", "pseudolife_memory.cli", *argv])
    assert dispatched == []
    with pytest.raises(pytest.fail.Exception, match="observed no public CLI call"):
        finalizers[0]()


def test_prompt_subprocess_route_keeps_unrelated_calls_and_requires_observation(monkeypatch):
    import sys
    dispatched = []
    finalizers = prompt_subprocess_request(monkeypatch, dispatched)
    command = [sys.executable, "-c", "pass"]
    subprocess.run(command, timeout=2)
    assert dispatched == [((command,), {"timeout": 2})]
    with pytest.raises(pytest.fail.Exception, match="observed no public CLI call"):
        finalizers[0]()


def test_automatic_cli_selection_includes_admissions_once_in_stable_order(monkeypatch):
    historical = {node for node, mode in pytest_plugin.MANIFEST["mapped"].items()
                  if mode == "cli-main-process"}
    assert len(historical) == 8 and len(pytest_plugin.CLI_SUBPROCESS_NODES) == 18
    expected = sorted(historical | set(pytest_plugin.CLI_SUBPROCESS_NODES))
    assert len(expected) == 26 and cli_dispatch.selected_nodes() == expected
    # An admission overlapping the historical map must not produce duplicate JUnit nodes.
    monkeypatch.setitem(pytest_plugin.CLI_SUBPROCESS_NODES, next(iter(historical)), ["--help"])
    assert cli_dispatch.selected_nodes() == expected


def test_cli_runtime_uses_selected_source_version(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.17.0"\n', encoding="utf-8")
    runtime = {"source_origin_matches_selected_root": True, "package_runtime_version": "0.17.0",
               "distribution_versions": {"pseudolife-mcp": "0.17.0"}}
    monkeypatch.setattr(cli_dispatch, "runtime_metadata", lambda root: runtime)
    assert cli_dispatch.require_cli_runtime(tmp_path) is runtime


@pytest.mark.parametrize("field,value", [
    ("source_origin_matches_selected_root", False), ("package_runtime_version", "0.16.1"),
    ("distribution_versions", {"pseudolife-mcp": "0.16.1"}),
    ("distribution_versions", {"pseudolife-mcp": None}),
])
def test_cli_runtime_rejects_source_and_version_drift(tmp_path, monkeypatch, field, value):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.17.0"\n', encoding="utf-8")
    runtime = {"source_origin_matches_selected_root": True, "package_runtime_version": "0.17.0",
               "distribution_versions": {"pseudolife-mcp": "0.17.0"}, field: value}
    monkeypatch.setattr(cli_dispatch, "runtime_metadata", lambda root: runtime)
    with pytest.raises(RuntimeError, match="genuine pinned installed package runtime"):
        cli_dispatch.require_cli_runtime(tmp_path)


def test_cli_dispatch_rejects_unpinned_source_before_runtime_or_candidate(monkeypatch, tmp_path):
    def unpinned(root):
        raise RuntimeError("phase-1 pinned production source required")
    monkeypatch.setattr(cli_dispatch, "require_phase1_source", unpinned)
    monkeypatch.setattr(cli_dispatch, "require_cli_runtime", lambda root: pytest.fail("must reject source first"))
    monkeypatch.setattr(cli_dispatch, "candidate_identity", lambda *args: pytest.fail("must reject source first"))
    with pytest.raises(RuntimeError, match="pinned production source required"):
        cli_dispatch.run(tmp_path, ["candidate"], tmp_path, tmp_path, {})
