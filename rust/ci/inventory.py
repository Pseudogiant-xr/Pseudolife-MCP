"""Check executable CI selections against the accepted row/control registries."""

import ast
import json
import re
from pathlib import Path

from test_workflow import commands

ROOT = Path(__file__).resolve().parents[2]
SYSTEMS = ["ubuntu-latest", "windows-latest"]


def cli_controls():
    result = {}
    for path in (ROOT / "rust/cli_harness/rows").glob("*.py"):
        ids = {node.args[0].value for node in ast.walk(ast.parse(path.read_text()))
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == "Mutant"}
        if ids:
            result[path.stem] = ids
    return result


def assigned_list(path, name):
    tree = ast.parse((ROOT / path).read_text())
    return next(ast.literal_eval(node.value) for node in tree.body
                if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == name for target in node.targets))


def daemon_scenarios():
    tree = ast.parse((ROOT / 'rust/daemon/harness/run.py').read_text())
    classes = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for field in node.body:
                if isinstance(field, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'name'
                                                        for t in field.targets):
                    classes[node.name] = ast.literal_eval(field.value)
    registry = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == 'SCENARIOS' for t in node.targets))
    return {classes[node.id] for node in registry.generators[0].iter.elts}


def check(workflow):
    from coverage_plan import SUITES, DAEMON_CONTROLS, GRAPH_CONTROLS, W1A, EXCEPTIONS, SOURCE_SHARDS

    declared = cli_controls()
    selected = json.loads((ROOT / "rust/ci/source_mutants.json").read_text())
    assert {row: set(ids) for row, ids in selected.items()} == declared, "source mutant inventory drift"
    rows = assigned_list("rust/cli_harness/rows/__init__.py", "ROWS")
    assert set(rows) == set(selected), "accepted row without a source-control CI selection"
    jobs = workflow["jobs"]
    parity = jobs["parity-checks"]
    matrix = parity["strategy"]["matrix"]
    assert matrix["os"] == SYSTEMS and "exclude" not in matrix
    assert set(SUITES) <= set(matrix["suite"]), "missing H5 suite"
    step = next(s for s in parity["steps"] if s.get("name") == "Ported rows and daemon controls")
    assert step["if"] == "startsWith(matrix.suite, 'h5-')"
    assert not step.get("continue-on-error")
    assert ["python", "rust/ci/run_coverage.py", "--suite", "${{", "matrix.suite", "}}"] \
        == commands(step["run"])[0][:6]
    assert "--out-dir" in commands(step["run"])[0]
    setup = next(s for s in parity["steps"] if s.get("name") == "Prepare disposable PostgreSQL for H5")
    assert setup["if"] == step["if"] and parity["steps"].index(setup) < parity["steps"].index(step)

    covered = {"audit", "backup"}
    for current in parity["steps"]:
        for words in commands(current.get("run", "")):
            if words[:2] == ["python", "rust/cli_harness"] and "--mutants" not in words:
                covered.update(words[i + 1] for i, word in enumerate(words[:-1]) if word == "--row")
    for shard in matrix["include"]:
        covered.update(re.findall(r"'([a-z_]+)'", shard.get("w1c_rows", "")))
    assert set(rows) <= covered, "accepted live row dropped"
    # Existing live/golden routing remains checked by test_workflow.py;
    # the executable H5 plan names its additional row modes explicitly.
    for row in ("audit", "backup"):
        assert {action["mode"] for action in SUITES[f"h5-{row}"]} == {"live", "record", "golden"}
        assert all(action["row"] == row for action in SUITES[f"h5-{row}"])

    source = jobs["source-mutants"]
    assert source["strategy"]["matrix"]["os"] == SYSTEMS
    assert set(source["strategy"]["matrix"]["shard"]) == set(SOURCE_SHARDS), "source mutant shard dropped"
    assert "exclude" not in source["strategy"]["matrix"]
    assert not source.get("continue-on-error")
    assert "source-mutants" in jobs["parity"]["needs"]
    assert source["if"] == "github.event_name != 'workflow_dispatch' || inputs.mode != 'wait-mail'"
    invocation = next(s for s in source["steps"] if s.get("name") == "CLI source mutants")
    assert not invocation.get("if") and not invocation.get("continue-on-error")
    assert commands(invocation["run"])[0][:6] == ["python", "rust/ci/run_coverage.py", "--source-shard", "${{", "matrix.shard", "}}"]
    oracle = next(s for s in source['steps'] if s.get('name') == 'Bind the source-control Python oracle')
    assert source['steps'].index(oracle) < source['steps'].index(invocation)
    assert oracle['env'] == {'GITHUB_REF': '${{ github.ref }}'}
    assert commands(oracle['run'])[0][:3] == ['python', '-m', 'evals.rust_port.oracle_selection']
    assert '--github-env' in commands(oracle['run'])[0]

    generic = set(assigned_list("rust/daemon/harness/run.py", "MUTANTS"))
    assert generic == set(DAEMON_CONTROLS) | set(EXCEPTIONS["daemon_mutants"]), "daemon control dropped"
    executed = {action["mutant"] for action in SUITES["h5-daemon-mutants"]}
    assert set(DAEMON_CONTROLS) == executed, ("daemon source mutant dropped", set(DAEMON_CONTROLS) - executed)
    for row, path in (("store", "graph_store.py"), ("read", "graph_read_profile.py")):
        assert any(action == {"kind": "graph", "mode": "mutants", "row": row}
                   for action in SUITES["h5-graph-mutants"])
        assert assigned_list("rust/daemon/harness/" + path, "MUTANTS") == GRAPH_CONTROLS[row]
    assert set(W1A) == {action["scenario"] for action in SUITES["h5-w1a"] if action["mode"] == "live"} \
        | set(EXCEPTIONS["w1a"]), "W1-A scenario dropped"
    existing = set()
    for step in parity['steps']:
        for words in commands(step.get('run', '')):
            if words[:3] == ['python', 'rust/daemon/harness/run.py', 'live'] and '--only' in words:
                existing.update(words[words.index('--only') + 1:])
    assert daemon_scenarios() <= existing | set(W1A), 'accepted daemon scenario missing from CI inventory'
    assert any(s.get("name") == "ported-row-and-mutant-ci-inventory" for s in jobs["rust"]["steps"])


if __name__ == "__main__":
    import yaml
    check(yaml.safe_load((ROOT / ".github/workflows/rust.yml").read_text()))
    print("ported-row-and-mutant-ci-inventory: all named selections accounted for")
