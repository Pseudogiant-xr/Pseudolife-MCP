"""Offline policy controls; native execution evidence is collected separately."""
import argparse
import ast
import base64
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest

from evals.rust_port import cli_briefing_hook


def test_historical_cases_remain_the_original_prefix():
    old = cli_briefing_hook.cases()
    new = cli_briefing_hook.reduction_cases()
    assert new[:len(old)] == old
    assert len({case["id"] for case in new}) == len(new)


def test_native_substitutions_keep_raw_python_expectations():
    cases = {case["id"]: case for case in cli_briefing_hook.reduction_cases()}
    env = {"PSEUDOLIFE_MCP_TOKEN": "synthetic"}
    for name in ["prompt-json-nan-extra", "prompt-json-surrogate-extra"]:
        raw = cli_briefing_hook.expected(cases[name], env)
        native = cli_briefing_hook.native_expected(cases[name], env)
        assert len(raw["wire"]) == 1
        assert native["wire"] == [] and native["marks"] == {}
        assert native["exit_code"] == 0 and native["stdout_b64"] == ""
        assert native["policies"] == ["hook-strict-json-refusal"]
    case = cases["prompt-mark-nonascii-after-lf"]
    assert "&since=" not in cli_briefing_hook.expected(case, env)["wire"][0]["target"]
    assert cli_briefing_hook.native_expected(case, env)["wire"][0]["target"].endswith("&since=100.0")
    malformed = cases["prompt-mark-invalid-ascii"]
    assert "&since=" not in cli_briefing_hook.native_expected(malformed, env)["wire"][0]["target"]


def test_beyond_u64_ignored_metadata_keeps_output_and_request():
    cases = {case["id"]: case for case in cli_briefing_hook.reduction_cases()}
    for name in ["briefing-json-beyond-u64-extra", "prompt-json-beyond-u64-extra"]:
        native = cli_briefing_hook.native_expected(cases[name], {})
        assert native["exit_code"] == 0 and native["wire"]
        assert native["policies"] == []
    assert base64.b64decode(cli_briefing_hook.native_expected(cases["briefing-json-beyond-u64-extra"], {})["stdout_b64"]).strip() == b"ok"


def test_non_json_health_stays_quiet_without_payload_request():
    case = next(case for case in cli_briefing_hook.cases() if case["id"] == "briefing-health-non-json")
    raw = cli_briefing_hook.expected(case, {})
    native = cli_briefing_hook.native_expected(case, {})
    assert native == {**raw, "policies": []}
    assert native["exit_code"] == 0 and native["stdout_b64"] == native.get("stderr_b64", "") == ""
    assert [request["target"] for request in native["wire"]] == ["/health"]


@pytest.mark.parametrize("name", ["briefing-token-control", "briefing-token-del", "briefing-token-fold"])
def test_forbidden_bearer_candidate_disposition_keeps_raw_python_observation(name):
    case = next(case for case in cli_briefing_hook.cases() if case["id"] == name)
    raw = cli_briefing_hook.expected(case, {})
    native = cli_briefing_hook.native_expected(case, {})
    assert len(raw["wire"]) == 2
    assert [request["target"] for request in native["wire"]] == ["/health"]
    assert native["exit_code"] == 0 and native["stdout_b64"] == ""
    assert native["policies"] == ["http-forbidden-input-refused"]


@pytest.mark.parametrize("name", [
    "briefing-json-nan-extra", "briefing-json-infinity-extra", "briefing-json-surrogate-extra",
    "briefing-json-malformed", "briefing-json-deep-extra", "briefing-markdown-false",
    "briefing-markdown-zero", "briefing-markdown-list", "briefing-markdown-object",
])
def test_briefing_refusal_has_exact_terminal_output_and_no_followup(name):
    case = next(case for case in cli_briefing_hook.reduction_cases() if case["id"] == name)
    result = cli_briefing_hook.native_expected(case, {})
    assert result["exit_code"] == 1 and result["stdout_b64"] == ""
    assert [cell["target"] for cell in result["wire"]] == ["/health", "/api/briefing?max_unsure=3&max_lessons=3&max_world=3"]
    assert base64.b64decode(result["stderr_b64"]).replace(b"\r\n", b"\n") == b"pseudolife-mcp briefing: daemon reply not understood\n"
    if name == "briefing-json-deep-extra":
        assert result["policies"] == ["hook-bounded-json-nesting"]


def test_briefing_help_asset_matches_pinned_python_at_columns80(monkeypatch, capsys):
    root = Path(__file__).resolve().parents[2]
    source = subprocess.check_output([
        "git", "show", "eb0c13e9c5036aa2b95e7fccb77f41ca1c095493:pseudolife_memory/briefing_cli.py",
    ], cwd=root).decode("utf-8")
    function = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "run_briefing")
    shim = ModuleType("pseudolife_memory.shim")
    shim._daemon_url = lambda: pytest.fail("help must not discover a daemon")
    shim.probe_health = lambda url: pytest.fail("help must not query a daemon")
    monkeypatch.setitem(sys.modules, "pseudolife_memory.shim", shim)
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "briefing", "--help"])
    monkeypatch.setenv("COLUMNS", "80")
    namespace = {"argparse": argparse, "os": os, "sys": sys}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "pinned-briefing-help", "exec"), namespace)
    with pytest.raises(SystemExit) as ended:
        namespace["run_briefing"]()
    assert ended.value.code == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out == (root / "rust/shim/src/cli/briefing_help.txt").read_text(encoding="utf-8")
