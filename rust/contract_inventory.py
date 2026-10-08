"""Audit the pinned port inventory without importing the Python daemon."""
from __future__ import annotations

import ast
from collections import Counter, defaultdict
from functools import cache, partial
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ORACLE = "3691f5cb75487d3fda54a6bde6fab35dcf32c681"
PHASE1_ORACLE = "686b3f95c4a4d4e6c2d81e1b76be9901945a8da1"
PHASE1_FUNCTION_FILES = (
    "tests/test_shim.py", "tests/test_shim_transport_recovery.py",
    "tests/test_shim_board_retry.py", "tests/test_version_handshake.py",
    "tests/test_update_offer.py", "tests/test_mcp_client_neutrality.py",
    "tests/test_mcp_stdio_errlog.py", "tests/test_connection_loss_recovery.py",
    "tests/test_shim_channel.py",
)


def source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


@cache
def pinned_source(path: str, *, oracle: str = ORACLE) -> str:
    """Read immutable oracle blobs; register and manifest reads stay live."""
    return subprocess.check_output(["git", "show", f"{oracle}:{path}"],
                                   cwd=ROOT, text=True, encoding="utf-8-sig")


def snapshot(*, oracle: str = ORACLE) -> dict:
    read = partial(pinned_source, oracle=oracle)
    config = ast.parse(read("pseudolife_memory/utils/config.py"))
    sections = {
        node.name: [field.target.id for field in node.body
                    if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name)]
        for node in config.body if isinstance(node, ast.ClassDef)
        and any(isinstance(dec, ast.Name) and dec.id == "dataclass" for dec in node.decorator_list)
    }
    variables = defaultdict(set)
    # Conservative superset: include declarations/propagation and source comments
    # as well as direct reads, so dynamically selected env keys are not omitted.
    # Documentation and generated Console assets are not executable readers.
    extensions = {".py", ".sh", ".ps1", ".json", ".yml", ".yaml", ".toml", ".bat", ".cmd"}
    tracked = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", oracle, "pseudolife_memory", "ops", "plugin"],
                                      cwd=ROOT, text=True).splitlines()
    python_trees = {}
    env_symbols = {}
    for path in tracked:
        if Path(path).suffix not in extensions and not Path(path).name.startswith("Dockerfile"):
            continue
        text = read(path)
        for name in set(re.findall(r"(?<![A-Z0-9])_?PSEUDOLIFE_[A-Z][A-Z0-9_]*\b", text)):
            if name.endswith("_"):
                continue  # A prefix/family is not a concrete variable.
            variables[name].add(path)
        if Path(path).suffix == ".py":
            tree = ast.parse(text)
            python_trees[path] = tree
            module = path.removesuffix(".py").replace("/", ".")
            for node in tree.body:
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    value = node.value.value
                    if re.fullmatch(r"_?PSEUDOLIFE_[A-Z][A-Z0-9_]*", value) and not value.endswith("_"):
                        for target in node.targets:
                            if isinstance(target, ast.Name):
                                env_symbols[(module, target.id)] = value
    # Imported named env keys still have a reader even when the literal lives
    # in client_config.py rather than the consumer. Keep those paths explicit.
    for path, tree in python_trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    name = env_symbols.get((node.module, alias.name))
                    if name:
                        variables[name].add(path)
    # The autostart runner builds these keys from a kind prefix + key.upper().
    kinds = next(ast.literal_eval(node.value) for node in python_trees["ops/shim_autostart.py"].body
                 if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                 and node.targets[0].id == "KINDS")
    for kind in kinds.values():
        for key in kind["keys"]:
            variables[kind["prefix"] + key.upper()].add("ops/shim_autostart.py")
    tools = []
    for node in ast.parse(read("pseudolife_memory/mcp_server.py")).body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == "_tool":
                tier = next((ast.literal_eval(k.value) for k in dec.keywords if k.arg == "tier"), "full")
                tools.append({"name": node.name, "tier": tier})
    # Async tier switching is registered manually so it can emit listChanged.
    mcp_source = read("pseudolife_memory/mcp_server.py")
    for name, tier in re.findall(r'_TOOL_TIERS\["([^"]+)"\] = "([^"]+)"', mcp_source):
        if re.search(r'\)\(' + re.escape(name) + r'\)', mcp_source):
            tools.append({"name": name, "tier": tier})
    routes = [{"method": "GET" if m == "g" else "POST", "path": path}
              for m, path in re.findall(r'\b([gp])\("(/api/[^"\n]+)"', read("pseudolife_memory/web/routes.py"))]
    cli = ast.parse(read("pseudolife_memory/cli.py"))
    modes = set()
    for node in ast.walk(cli):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "mode":
            for value in ast.walk(node.comparators[0]):
                if isinstance(value, ast.Constant) and isinstance(value.value, str) and not value.value.startswith("-"):
                    modes.add(value.value)
    api = read("pseudolife_memory/web/api.py")
    hooks = [{"path": path, "method": method} for path, method in re.findall(
        r'if path == "(/api/hook/[^"]+)":.*?if method != "([A-Z]+)"', api, re.S)]
    actions = next([key.value for key in node.value.keys]
                   for node in ast.parse(read("pseudolife_memory/coordination.py")).body
                   if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                   and node.targets[0].id == "_PARAMETERS")
    schema = next(ast.literal_eval(node.value)
                  for node in ast.parse(read("pseudolife_memory/storage/schema.py")).body
                  if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "SCHEMA_META_VERSION"
                          for target in node.targets))
    return {"schema": 1, "oracle_commit": oracle, "database_schema": schema,
            "config_sections": sections,
            "environment_variables": {key: sorted(value) for key, value in sorted(variables.items())},
            "tools": tools, "console_routes": routes, "cli_modes": sorted(modes),
            "hook_endpoints": hooks, "coordination_actions": actions}


def test_files_at_pin(*, oracle: str = ORACLE) -> list[str]:
    paths = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", oracle, "tests"],
                                    cwd=ROOT, text=True).splitlines()
    return sorted(path for path in paths if re.fullmatch(r"tests/test_[^/]+\.py", path))


def literal_node_ids(path: str, *, pinned: bool = False, oracle: str = ORACLE) -> set[str]:
    """Resolve concrete IDs for the adapter's literal, single-axis CLI cases.

    Fail closed for any unsupported parameterization, rather than pretending
    a function name proves a concrete pytest node exists.
    """
    result = set()
    for node in ast.parse(pinned_source(path, oracle=oracle) if pinned else source(path)).body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test_"):
            continue
        params = [dec for dec in node.decorator_list if isinstance(dec, ast.Call)
                  and isinstance(dec.func, ast.Attribute) and dec.func.attr == "parametrize"]
        if not params:
            result.add(f"{path}::{node.name}")
        elif len(params) == 1 and len(params[0].args) == 2 and not params[0].keywords:
            names, values = map(ast.literal_eval, params[0].args)
            if "," not in names and all(isinstance(value, str) for value in values):
                result.update(f"{path}::{node.name}[{value}]" for value in values)
    return result


@cache
def phase1_function_sources(*, oracle: str = PHASE1_ORACLE, root: Path = ROOT) -> dict:
    """Enumerate every scoped test function from exact pinned Git bytes."""
    result = {}
    for path in PHASE1_FUNCTION_FILES:
        raw = subprocess.check_output(["git", "show", f"{oracle}:{path}"], cwd=root)
        digest = hashlib.sha256(raw).hexdigest()
        for node in ast.parse(raw).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                result[f"{path}::{node.name}"] = {"line": node.lineno, "source_sha256": digest}
    return result


def validate_phase1_functions(functions: list[dict], *, oracle: str = PHASE1_ORACLE,
                              root: Path = ROOT) -> None:
    expected = phase1_function_sources(oracle=oracle, root=root)
    nodes = [item["nodeid"] for item in functions]
    assert len(nodes) == len(set(nodes)), "duplicate per-function Phase 1 ownership"
    assert set(nodes) == set(expected), "missing or extra per-function Phase 1 ownership"
    for item in functions:
        identity = expected[item["nodeid"]]
        assert item["source_sha256"] == identity["source_sha256"], f"stale per-function source hash: {item['nodeid']}"
        assert item["line"] == identity["line"], f"stale per-function source line: {item['nodeid']}"


def validate(*, phase1: bool = False) -> dict:
    """Audit Phase 0b by default; Phase 1 has separate pinned manifests."""
    oracle = PHASE1_ORACLE if phase1 else ORACLE
    inventory_path = "rust/phase1-contract-inventory.json" if phase1 else "rust/contract-inventory.json"
    buckets_path = "rust/phase1-test-buckets.json" if phase1 else "rust/test-buckets.json"
    mapping_path = "evals/rust_port/oracle_tests.json" if phase1 else "rust/phase0b-oracle-tests.json"
    inventory = json.loads(source(inventory_path))
    expected = snapshot(oracle=oracle) if phase1 else snapshot()
    assert inventory == expected, "contract snapshot differs from pinned source inventory"
    manifest = json.loads(source(buckets_path))
    assert manifest["oracle_commit"] == oracle
    files = manifest["files"]
    paths = [item["path"] for item in files]
    assert len(paths) == len(set(paths)), "duplicate test-file bucket"
    pinned_files = test_files_at_pin(oracle=oracle)
    assert sorted(paths) == pinned_files, "missing or extra test-file bucket"
    mapping = json.loads(source(mapping_path))
    if phase1:
        assert mapping["oracle_commit"] == oracle, "adapter mapping differs from Phase 1 oracle pin"
    mapped = mapping["mapped"]
    selected = []
    for item in files:
        assert item["bucket"] in {"oracle", "candidate", "internal"}
        if item["bucket"] == "candidate":
            assert item["candidate_nodes"]
            assert item["remaining_nodes"] == "oracle"
            existing = literal_node_ids(item["path"])
            pinned_nodes = literal_node_ids(item["path"], pinned=True, oracle=oracle)
            for node in item["candidate_nodes"]:
                assert node in existing, f"candidate node does not exist: {node}"
                assert node in pinned_nodes, f"candidate node does not exist at oracle pin: {node}"
                assert node in mapped, f"external plugin cannot route: {node}"
                selected.append(node)
        elif item["bucket"] == "internal":
            equivalent = item["equivalent"]
            assert isinstance(equivalent, str) and (equivalent == "pending" or equivalent.startswith(("rust-unit:", "wire-case:"))), "unnamed internal equivalent"
    assert len(selected) == len(set(selected)), "duplicate candidate node"
    assert set(selected) == set(mapped), "adapter mapping and inventory disagree"
    counts = dict(Counter(item["bucket"] for item in files))
    assert manifest["counts"] == counts
    assert manifest["candidate_node_count"] == len(selected)
    if phase1:
        functions = manifest.get("phase1_functions", [])
        validate_phase1_functions(functions, oracle=oracle)
        candidates = {item["nodeid"] for item in functions if item["bucket"] == "candidate"}
        scoped_candidates = {node for node in selected if node.split("::", 1)[0] in PHASE1_FUNCTION_FILES}
        assert candidates == scoped_candidates, "candidate function ownership and adapter mapping disagree"
        scoped_internal = [item for item in functions
                           if item["bucket"] == "internal" and item["scope"] == "phase1"]
        assert len(scoped_internal) == 125, "missing scoped internal ownership"
        for item in functions:
            retired_sdk_nodes = {
                "tests/test_shim.py::test_sdk_guard_passes_on_a_v2_environment",
                "tests/test_shim.py::test_sdk_guard_names_the_fix_and_exits_before_daemon_traffic",
                "tests/test_shim.py::test_sdk_guard_survives_a_fully_absent_mcp",
            }
            if item["nodeid"] in retired_sdk_nodes:
                evidence = item.get("equivalence_evidence", {})
                assert (item["acceptance"] == "retired-by-decision"
                        and item["equivalent"] == "retired-by-decision"
                        and item["required_equivalent"] is None
                        and evidence.get("status") == "retired-by-decision"
                        and evidence.get("decision") == "Maintainer 2026-10-05 phase 2 brief, section 1; PORTING.md no-python-before-first-frame"
                        and evidence.get("rust_tests") == []), "SDK retirement differs from the maintainer decision"
                continue
            assert item["acceptance"] == "pending", "Phase 1 acceptance requires execution evidence"
            if item["required_equivalent"]:
                assert item["equivalent"] == "pending", "proposed target is not a passed equivalent"
            elif item in scoped_internal:
                evidence = item.get("equivalence_evidence", {})
                targets = evidence.get("rust_tests", [])
                assert (evidence.get("status") == "validated-targeted" and targets
                        and evidence.get("assertions")
                        and all(evidence.get("validation", {}).get(platform)
                                for platform in ("windows", "linux"))), "missing targeted equivalence evidence"
                assert item["equivalent"] == f"rust-unit:{targets[0]}", "equivalent target disagrees with evidence"
                for target in targets:
                    rust_path, *rust_names = target.split("::")
                    assert rust_names, "equivalent Rust function is absent"
                    rust_function = rust_names[-1]
                    assert (rust_path.startswith("rust/") and rust_path.endswith(".rs")
                            and ".." not in Path(rust_path).parts
                            and (ROOT / rust_path).is_file()
                            and re.search(rf"\bfn\s+{re.escape(rust_function)}\s*\(", source(rust_path))), "equivalent Rust function is absent"
        assert manifest["phase1_function_counts"] == dict(Counter(item["bucket"] for item in functions))
    register = source("rust/PARITY.md")
    assert (f"{len(files)} `tests/test_*.py` files: {counts['oracle']} oracle, "
            f"{counts['candidate']} candidate and {counts['internal']} internal") in register, "stale bucket counts"
    for name in inventory["config_sections"]:
        assert f"`{name}`" in register, f"missing config section: {name}"
    for name in inventory["environment_variables"]:
        assert f"`{name}`" in register, f"missing environment variable: {name}"
    for tool in inventory["tools"]:
        assert f"| {tool['name']} |" in register, f"missing MCP tool: {tool['name']}"
    for route in inventory["console_routes"]:
        assert f"| {route['method']} | {route['path']} |" in register, f"missing route: {route}"
    for mode in inventory["cli_modes"]:
        assert f"| {mode} |" in register, f"missing CLI mode: {mode}"
    for hook in inventory["hook_endpoints"]:
        assert f"| {hook['method']} {hook['path']} |" in register, f"missing hook method: {hook}"
    for action in inventory["coordination_actions"]:
        assert f"| {action} |" in register, f"missing coordination action: {action}"
    assert "| POST | /api/pair |" in register, "missing pairing route"
    required_rows = {"CONFIG-LOAD", "BACKGROUND-SWEEP", "BACKGROUND-SESSIONS", "HEALTH",
                     "MCP-MOUNT", "DISPOSABLE-GUARDS", "SEARCH-COMPACTION", "CONTRADICTION",
                     "HTTP-BODY-LIMITS", "PRINCIPALS-V53", "CLI-PAIRING", "CLI-EXPOSE-MOVE",
                     "MAIL-RING-V53", "OPS-INSTALL-RESTORE", "OPS-IMAGES", "PLUGIN-COMMANDS",
                     "ONNX-PREREQUISITE"}
    for row in required_rows:
        assert f"| {row} |" in register, f"missing required surface row: {row}"
    status_index = mode_index = surface_index = None
    checked_cli_modes = set()
    checked_surface_rows = set()
    for line in register.splitlines():
        if not line.startswith("|"):
            status_index = mode_index = surface_index = None
            continue
        if line.startswith("|---"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if "Status" in cells:
            status_index = cells.index("Status")
            mode_index = cells.index("Mode") if "Mode" in cells else None
            surface_index = cells.index("Row") if "Row" in cells else None
        elif status_index is not None:
            assert cells[status_index] in {"ported", "ported-with-substitution", "deferred", "retired-by-decision"}, f"invalid parity status: {line}"
            if mode_index is not None:
                checked_cli_modes.add(cells[mode_index])
            if surface_index is not None:
                checked_surface_rows.add(cells[surface_index])
    for row in required_rows:
        assert row in checked_surface_rows, f"surface row outside status table: {row}"
    for mode in inventory["cli_modes"]:
        assert mode in checked_cli_modes, f"CLI mode outside status table: {mode}"
    # The shared register names both documented oracle snapshots. Candidate
    # and function ownership above remains checked against its own exact pin.
    register_files = set(pinned_files)
    if not phase1:
        register_files.update(test_files_at_pin(oracle=PHASE1_ORACLE))
    for name in set(re.findall(r'`(?:tests/)?(test_[A-Za-z0-9_]+\.py)`', register)):
        assert f"tests/{name}" in register_files, f"obsolete test reference: {name}"
    return {"test_files": len(files), "buckets": counts, "candidate_nodes": len(selected),
            "config_sections": len(inventory["config_sections"]),
            "environment_variables": len(inventory["environment_variables"]),
            "tools": len(inventory["tools"]), "console_routes": len(inventory["console_routes"]),
            "cli_modes": len(inventory["cli_modes"]),
            "hook_endpoints": len(inventory["hook_endpoints"]),
            "coordination_actions": len(inventory["coordination_actions"]), "missing_surfaces": []}


def validate_phase1() -> dict:
    """Validate the current Phase 1 inventory at its explicit oracle pin."""
    return validate(phase1=True)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase1", action="store_true", help="audit the separate Phase 1 inventory")
    print(json.dumps(validate(phase1=parser.parse_args().phase1), sort_keys=True))
