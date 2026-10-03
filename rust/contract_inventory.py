"""Audit the pinned port inventory without importing the Python daemon."""
from __future__ import annotations

import ast
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ORACLE = "3691f5cb75487d3fda54a6bde6fab35dcf32c681"


def source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def snapshot() -> dict:
    config = ast.parse(source("pseudolife_memory/utils/config.py"))
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
    tracked = subprocess.check_output(["git", "ls-files", "pseudolife_memory", "ops", "plugin"],
                                      cwd=ROOT, text=True).splitlines()
    python_trees = {}
    env_symbols = {}
    for path in tracked:
        if Path(path).suffix not in extensions and not Path(path).name.startswith("Dockerfile"):
            continue
        text = source(path)
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
    for node in ast.parse(source("pseudolife_memory/mcp_server.py")).body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == "_tool":
                tier = next((ast.literal_eval(k.value) for k in dec.keywords if k.arg == "tier"), "full")
                tools.append({"name": node.name, "tier": tier})
    # Async tier switching is registered manually so it can emit listChanged.
    mcp_source = source("pseudolife_memory/mcp_server.py")
    for name, tier in re.findall(r'_TOOL_TIERS\["([^"]+)"\] = "([^"]+)"', mcp_source):
        if re.search(r'\)\(' + re.escape(name) + r'\)', mcp_source):
            tools.append({"name": name, "tier": tier})
    routes = [{"method": "GET" if m == "g" else "POST", "path": path}
              for m, path in re.findall(r'\b([gp])\("(/api/[^"\n]+)"', source("pseudolife_memory/web/routes.py"))]
    cli = ast.parse(source("pseudolife_memory/cli.py"))
    modes = set()
    for node in ast.walk(cli):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "mode":
            for value in ast.walk(node.comparators[0]):
                if isinstance(value, ast.Constant) and isinstance(value.value, str) and not value.value.startswith("-"):
                    modes.add(value.value)
    api = source("pseudolife_memory/web/api.py")
    hooks = [{"path": path, "method": method} for path, method in re.findall(
        r'if path == "(/api/hook/[^"]+)":.*?if method != "([A-Z]+)"', api, re.S)]
    actions = next([key.value for key in node.value.keys]
                   for node in ast.parse(source("pseudolife_memory/coordination.py")).body
                   if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                   and node.targets[0].id == "_PARAMETERS")
    return {"schema": 1, "oracle_commit": ORACLE, "database_schema": 53,
            "config_sections": sections,
            "environment_variables": {key: sorted(value) for key, value in sorted(variables.items())},
            "tools": tools, "console_routes": routes, "cli_modes": sorted(modes),
            "hook_endpoints": hooks, "coordination_actions": actions}


def test_files_at_pin() -> list[str]:
    paths = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", ORACLE, "tests"],
                                    cwd=ROOT, text=True).splitlines()
    return sorted(path for path in paths if re.fullmatch(r"tests/test_[^/]+\.py", path))


def literal_node_ids(path: str) -> set[str]:
    """Resolve concrete IDs for the adapter's literal, single-axis CLI cases.

    Fail closed for any unsupported parameterization, rather than pretending
    a function name proves a concrete pytest node exists.
    """
    result = set()
    for node in ast.parse(source(path)).body:
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


def validate() -> dict:
    inventory = json.loads(source("rust/contract-inventory.json"))
    assert inventory == snapshot(), "contract snapshot differs from pinned source inventory"
    manifest = json.loads(source("rust/test-buckets.json"))
    assert manifest["oracle_commit"] == ORACLE
    files = manifest["files"]
    paths = [item["path"] for item in files]
    assert len(paths) == len(set(paths)), "duplicate test-file bucket"
    assert sorted(paths) == test_files_at_pin(), "missing or extra test-file bucket"
    assert sorted(paths) == sorted(path.relative_to(ROOT).as_posix() for path in (ROOT / "tests").glob("test_*.py"))
    mapped = json.loads(source("evals/rust_port/oracle_tests.json"))["mapped"]
    selected = []
    for item in files:
        assert item["bucket"] in {"oracle", "candidate", "internal"}
        if item["bucket"] == "candidate":
            assert item["candidate_nodes"]
            assert item["remaining_nodes"] == "oracle"
            existing = literal_node_ids(item["path"])
            for node in item["candidate_nodes"]:
                assert node in existing, f"candidate node does not exist: {node}"
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
    status_index = mode_index = None
    checked_cli_modes = set()
    for line in register.splitlines():
        if not line.startswith("|"):
            status_index = mode_index = None
            continue
        if line.startswith("|---"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if "Status" in cells:
            status_index = cells.index("Status")
            mode_index = cells.index("Mode") if "Mode" in cells else None
        elif status_index is not None:
            assert cells[status_index] in {"ported", "deferred", "retired-by-decision"}, f"invalid parity status: {line}"
            if mode_index is not None:
                checked_cli_modes.add(cells[mode_index])
    for mode in inventory["cli_modes"]:
        assert mode in checked_cli_modes, f"CLI mode outside status table: {mode}"
    for name in set(re.findall(r'`(?:tests/)?(test_[A-Za-z0-9_]+\.py)`', register)):
        assert (ROOT / "tests" / name).is_file(), f"obsolete test reference: {name}"
    return {"test_files": len(files), "buckets": counts, "candidate_nodes": len(selected),
            "config_sections": len(inventory["config_sections"]),
            "environment_variables": len(inventory["environment_variables"]),
            "tools": len(inventory["tools"]), "console_routes": len(inventory["console_routes"]),
            "cli_modes": len(inventory["cli_modes"]),
            "hook_endpoints": len(inventory["hook_endpoints"]),
            "coordination_actions": len(inventory["coordination_actions"]), "missing_surfaces": []}


if __name__ == "__main__":
    print(json.dumps(validate(), sort_keys=True))
