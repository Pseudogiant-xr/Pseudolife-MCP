"""Phase 2 CLI admission and byte comparison controls."""
import ast
import base64
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from evals.rust_port import cli_corpus, harness, pytest_plugin


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
    assert pytest_plugin.boundary("tests/test_cli_dispatch.py::test_version_from_a_runtime_names_its_directory_and_commit") is None


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
