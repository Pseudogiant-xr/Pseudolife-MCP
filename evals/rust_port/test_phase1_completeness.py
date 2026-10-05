"""Current-pin ownership and public routes must cover the scoped source."""
import ast
import asyncio
from collections import Counter
from contextlib import asynccontextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import mcp.client.stdio
from mcp import StdioServerParameters
import pytest

from evals.rust_port import phase1_inventory, pytest_plugin


SCOPED_FILES = (
    "tests/test_shim.py", "tests/test_shim_transport_recovery.py",
    "tests/test_shim_board_retry.py", "tests/test_version_handshake.py",
    "tests/test_update_offer.py", "tests/test_mcp_client_neutrality.py",
    "tests/test_mcp_stdio_errlog.py", "tests/test_connection_loss_recovery.py",
    "tests/test_shim_channel.py",
)
REFUSAL_NODES = (
    "tests/test_shim.py::test_shim_passes_a_tool_refusal_through_unchanged",
    "tests/test_shim.py::test_shim_forwards_the_daemons_unknown_parameter_refusal",
)


@pytest.fixture(scope="module")
def inventory():
    path = Path(__file__).resolve().parents[2] / "rust/contract_inventory.py"
    spec = importlib.util.spec_from_file_location("phase1_completeness_inventory", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_current_pin_function_has_exact_source_identity(inventory):
    expected = {}
    for path in SCOPED_FILES:
        raw = subprocess.check_output(["git", "show", f"{inventory.PHASE1_ORACLE}:{path}"],
                                      cwd=inventory.ROOT)
        for node in ast.parse(raw).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                expected[f"{path}::{node.name}"] = (node.lineno, hashlib.sha256(raw).hexdigest())
    functions = json.loads(inventory.source("rust/phase1-test-buckets.json"))["phase1_functions"]
    assert len(expected) == len(functions) == 191
    assert {item["nodeid"]: (item["line"], item["source_sha256"]) for item in functions} == expected


@pytest.mark.parametrize("fault,error", [
    ("missing", "missing or extra per-function Phase 1 ownership"),
    ("missing-current-public-nodes", "missing or extra per-function Phase 1 ownership"),
    ("extra", "missing or extra per-function Phase 1 ownership"),
    ("duplicate", "duplicate per-function Phase 1 ownership"),
    ("hash", "stale per-function source hash"),
    ("line", "stale per-function source line"),
])
def test_current_audit_rejects_incomplete_or_stale_function_entries(inventory, monkeypatch, fault, error):
    original = inventory.source
    manifest = json.loads(original("rust/phase1-test-buckets.json"))
    functions = manifest["phase1_functions"]
    if fault == "missing":
        functions.pop()
    elif fault == "missing-current-public-nodes":
        functions[:] = [item for item in functions if item["nodeid"] not in REFUSAL_NODES]
        manifest["phase1_function_counts"] = dict(Counter(item["bucket"] for item in functions))
    elif fault == "extra":
        functions.append({**functions[0], "nodeid": "tests/test_shim.py::test_unclassified_extra"})
    elif fault == "duplicate":
        functions[-1] = dict(functions[0])
    elif fault == "hash":
        functions[0]["source_sha256"] = "0" * 64
    else:
        functions[0]["line"] += 1
    monkeypatch.setattr(inventory, "source", lambda path: json.dumps(manifest)
                        if path == "rust/phase1-test-buckets.json" else original(path))
    with pytest.raises(AssertionError, match=error):
        inventory.validate_phase1()


def test_regeneration_rejects_omitted_current_functions_before_writing(inventory, monkeypatch, tmp_path):
    functions = json.loads(inventory.source("rust/phase1-test-buckets.json"))["phase1_functions"]
    exploration = tmp_path / "exploration.json"
    exploration.write_text(json.dumps({"files": [
        {"file": path, "functions": [item for item in functions
                                     if item["nodeid"].startswith(path + "::")
                                     and item["nodeid"] not in REFUSAL_NODES]}
        for path in SCOPED_FILES]}), encoding="utf-8")
    monkeypatch.setattr(phase1_inventory, "require_phase1_source", lambda root: None)
    monkeypatch.setattr(Path, "write_text", lambda *args, **kwargs: pytest.fail("incomplete input wrote a manifest"))
    with pytest.raises(AssertionError, match="missing or extra per-function Phase 1 ownership"):
        phase1_inventory.regenerate(exploration, root=inventory.ROOT)


def test_candidate_function_classification_cannot_drift_from_adapter_mapping(inventory, monkeypatch):
    original = inventory.source
    manifest = json.loads(original("rust/phase1-test-buckets.json"))
    item = next(item for item in manifest["phase1_functions"] if item["nodeid"] == REFUSAL_NODES[0])
    item["bucket"] = "oracle"
    monkeypatch.setattr(inventory, "source", lambda path: json.dumps(manifest)
                        if path == "rust/phase1-test-buckets.json" else original(path))
    with pytest.raises(AssertionError, match="candidate function ownership and adapter mapping disagree"):
        inventory.validate_phase1()


def test_current_mapping_pin_and_counts_match_pending_candidate_ownership(inventory):
    mapping = pytest_plugin.MANIFEST
    assert mapping["oracle_commit"] == inventory.PHASE1_ORACLE
    assert len(mapping["mapped"]) == 15
    assert sum(boundary == "stdio-shim-process" for boundary in mapping["mapped"].values()) == 10
    manifest = json.loads(inventory.source("rust/phase1-test-buckets.json"))
    functions = {item["nodeid"]: item for item in manifest["phase1_functions"]}
    for node in REFUSAL_NODES:
        assert mapping["mapped"][node] == "stdio-shim-process"
        item = functions[node]
        assert item["bucket"] == "candidate" and item["scope"] == "phase1"
        assert item["acceptance"] == "pending"
        assert item["equivalent"] is item["required_equivalent"] is None
        assert "equivalence_evidence" not in item
        assert "not executed" in item["reason"]


def test_current_mapping_rejects_an_old_pin(inventory, monkeypatch):
    original = inventory.source
    mapping = json.loads(original("evals/rust_port/oracle_tests.json"))
    mapping["oracle_commit"] = "f709abb54f7912ae9cd767998d0926ca33df4bcd"
    monkeypatch.setattr(inventory, "source", lambda path: json.dumps(mapping)
                        if path == "evals/rust_port/oracle_tests.json" else original(path))
    with pytest.raises(AssertionError, match="adapter mapping differs from Phase 1 oracle pin"):
        inventory.validate_phase1()


@pytest.mark.parametrize("nodeid", REFUSAL_NODES)
def test_refusal_nodes_keep_the_pinned_public_default_stdio_launch(inventory, nodeid):
    path, name = nodeid.split("::")
    tree = ast.parse(inventory.pinned_source(path, oracle=inventory.PHASE1_ORACLE))
    node = next(node for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    assert not node.decorator_list
    assert "shared_daemon" in {argument.arg for argument in node.args.args}
    launches = [call for call in ast.walk(node) if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name) and call.func.id == "StdioServerParameters"]
    assert len(launches) == 1
    arguments = {keyword.arg: keyword.value for keyword in launches[0].keywords}
    assert ast.unparse(arguments["command"]) == "sys.executable"
    assert ast.literal_eval(arguments["args"]) == ["-m", "pseudolife_memory.cli"]
    assert ast.unparse(arguments["env"]) == "env"
    assert any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
               and call.func.id == "_shim_env" for call in ast.walk(node))
    assert any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
               and call.func.id == "stdio_client" for call in ast.walk(node))


@pytest.mark.parametrize("nodeid", REFUSAL_NODES)
def test_refusal_nodes_route_to_candidate_and_preserve_sdk_streams_and_environment(monkeypatch, nodeid):
    streams = (object(), object())
    observed = []
    @asynccontextmanager
    async def original(server, *args, **kwargs):
        observed.append(server)
        yield streams
    monkeypatch.setattr(mcp.client.stdio, "stdio_client", original)
    monkeypatch.setenv("PSEUDOLIFE_MCP_PYTHON", sys.executable)
    config = SimpleNamespace(_port_stdio_prefix=["candidate", "fixed"], getoption=lambda name: False)
    pytest_plugin.pytest_collection_finish(SimpleNamespace(config=config, items=[SimpleNamespace(nodeid=nodeid)]))
    request = SimpleNamespace(config=config, node=SimpleNamespace(nodeid=nodeid),
                              getfixturevalue=lambda name: monkeypatch)
    pytest_plugin._port_selected_boundary.__wrapped__(request)
    env = {"PSEUDOLIFE_MCP_DAEMON_URL": "https://fixture.invalid",
           "PSEUDOLIFE_MCP_NO_SPAWN": "1", "PSEUDOLIFE_MCP_PYTHON": sys.executable}
    server = StdioServerParameters(command=sys.executable, args=["-m", "pseudolife_memory.cli"], env=env)
    async def run():
        async with mcp.client.stdio.stdio_client(server) as selected:
            assert selected is streams
    asyncio.run(run())
    assert len(observed) == 1
    assert observed[0].command == "candidate" and observed[0].args == ["fixed"]
    assert observed[0].env == env
    assert server.command == sys.executable and server.args == ["-m", "pseudolife_memory.cli"]
    assert server.env == env
