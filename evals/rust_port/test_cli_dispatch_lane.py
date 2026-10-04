"""Phase 2 CLI admission and byte comparison controls."""
import ast
import base64
from pathlib import Path
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


def test_help_source_matches_python_literal():
    root = Path(__file__).resolve().parents[2]
    tree = ast.parse((root / "pseudolife_memory/cli.py").read_text(encoding="utf-8"))
    usage = next(ast.literal_eval(node.value) for node in tree.body
                 if isinstance(node, ast.Assign) and any(
                     isinstance(target, ast.Name) and target.id == "_USAGE"
                     for target in node.targets))
    assert (root / "rust/shim/src/cli_help.txt").read_text(encoding="utf-8") == usage
