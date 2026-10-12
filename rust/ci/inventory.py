"""Check executable CI selections against the accepted row/control registries."""

import ast
from collections import Counter
import json
import re
import sys
from pathlib import Path

from test_workflow import commands

ROOT = Path(__file__).resolve().parents[2]
SYSTEMS = ["ubuntu-latest", "windows-latest"]
SOURCE_EVENTS = "github.event_name == 'push' || github.event_name == 'schedule' || (github.event_name == 'workflow_dispatch' && inputs.mode == 'ci')"
PARITY_EVENT_GATE = """
$results = ConvertFrom-Json $env:GATE_RESULTS -AsHashtable
foreach ($job in @('rust', 'candidate', 'parity-checks')) {
  if ($results[$job].result -cne 'success') {
    Write-Error "$job did not succeed"
    exit 1
  }
}
$sourceResult = $results['source-mutants'].result
if ($sourceResult -cne 'success' -and -not ($env:GATE_EVENT -ceq 'pull_request' -and $sourceResult -ceq 'skipped')) {
  Write-Error "source-mutants did not succeed outside the PR skip exception"
  exit 1
}
"""


def cli_controls():
    result = {}
    for path in (ROOT / "rust/cli_harness/rows").glob("*.py"):
        ids = {node.args[0].value for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == "Mutant"}
        if ids:
            result[path.stem] = ids
    return result


def assigned_list(path, name):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    return next(ast.literal_eval(node.value) for node in tree.body
                if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == name for target in node.targets))


def daemon_scenarios():
    sys.path.insert(0, str(ROOT / 'rust/daemon/harness'))
    import run
    return set(run.SCENARIOS)


def exception_reasons(value):
    if isinstance(value, dict):
        for reason in value.values():
            exception_reasons(reason)
    else:
        assert isinstance(value, str) and value.strip(), 'empty or invalid exception reason'


def exit_propagation(step, invocation):
    lines = [line.strip() for line in step['run'].splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    assert step.get('shell') == 'pwsh' and len(lines) == 2 \
        and lines[1] == 'exit $LASTEXITCODE', 'H5 exit propagation changed'
    assert lines[0] == invocation, 'H5 driver command or failure propagation changed'


def backup_zero_retention_scope(exceptions):
    scoped = exceptions['cli_case_selections']
    assert set(scoped) == {'windows-backup-zero-retention-clock-race'}, 'backup zero exception scope changed'
    entry = scoped['windows-backup-zero-retention-clock-race']
    assert set(entry) == {'platform', 'mode', 'row', 'case', 'reason'} \
        and (entry['platform'], entry['mode'], entry['row'], entry['case']) \
        == ('windows', 'both', 'backup', 'file-mode-keep-zero'), 'backup zero exception scope changed'
    assert all(proof in entry['reason'] for proof in ('GetSystemTimeAsFileTime', '0.015625',
                                                     '254/deleted 46', '229/deleted 71')), \
        'backup zero reference measurement missing'
    tree = ast.parse((ROOT / 'rust/cli_harness/rows/backup.py').read_text(encoding='utf-8'))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == 'Case'
             and node.args and isinstance(node.args[0], ast.Constant)
             and node.args[0].value == 'file-mode-keep-zero']
    assert len(calls) == 1, 'backup zero exception lost or duplicated case'
    options = {keyword.arg: keyword.value for keyword in calls[0].keywords}
    assert ast.literal_eval(options['platforms']) == ('linux',) \
        and ('golden' not in options or ast.literal_eval(options['golden']) is True) \
        and 'skip_if' not in options, 'backup zero Linux live/record/golden selection changed'
    assert isinstance(calls[0].args[1], ast.List) \
        and isinstance(calls[0].args[1].elts[-1], ast.Constant) \
        and calls[0].args[1].elts[-1].value == '--keep-days=0', 'backup zero argument changed'
    native = (ROOT / 'rust/shim/src/cli/backup/mod.rs').read_text(encoding='utf-8')
    for name in ('rotation_keeps_files_equal_to_the_python_clock_cutoff',
                 'windows_clock_matches_python_within_the_same_clock_tick'):
        assert re.search(r'#\[test\]\s+fn ' + name + r'\(', native), 'backup zero native proof dropped'


def check(workflow):
    from coverage_plan import (SUITES, DAEMON_CONTROLS, GRAPH_CONTROLS, W1A, EXCEPTIONS,
                               SOURCE_SHARDS, PLATFORM_EXCLUSIONS, UNOBSERVED_CONTROLS, BACKGROUND)
    from control_selection import runtime_controls
    from rust.cli_harness import core

    declared = cli_controls()
    selected = json.loads((ROOT / "rust/ci/source_mutants.json").read_text(encoding="utf-8"))
    assert {row: set(ids) for row, ids in selected.items()} == declared, "source mutant inventory drift"
    exception_reasons(EXCEPTIONS)
    backup_zero_retention_scope(EXCEPTIONS)
    exception_reasons(PLATFORM_EXCLUSIONS)
    exception_reasons(UNOBSERVED_CONTROLS)
    all_controls = set().union(*declared.values())
    for platform in ('linux', 'windows'):
        assert set(PLATFORM_EXCLUSIONS[platform]) <= all_controls, 'unknown platform exclusion'
    observed = set().union(*(all_controls - set(PLATFORM_EXCLUSIONS[p])
                             - set(EXCEPTIONS['cli_mutants'][p]) for p in ('linux', 'windows')))
    assert all_controls - observed == set(UNOBSERVED_CONTROLS), 'source control without any OS execution or named global exception'
    for row in declared:
        runtime_controls(row, core.PLATFORM)
    rows = assigned_list("rust/cli_harness/rows/__init__.py", "ROWS")
    assert set(rows) == set(selected), "accepted row without a source-control CI selection"
    # Payloads must retain every declaration before the dispatcher's platform
    # eligibility and named-exception filters, not merely retain shard names.
    assert set(row for row, ids in SOURCE_SHARDS.values()) == set(rows), 'executable source controls changed rows'
    executable = {row: Counter(mutant for owner, ids in SOURCE_SHARDS.values()
                               if owner == row for mutant in ids) for row in rows}
    assert executable == {row: Counter(ids) for row, ids in selected.items()}, 'executable source controls dropped or duplicated'
    assert all(set(exceptions) <= set().union(*declared.values())
               for exceptions in EXCEPTIONS['cli_mutants'].values()), 'unknown source-control exception'
    jobs = workflow["jobs"]
    parity = jobs["parity-checks"]
    ordinary_ci = "github.event_name != 'workflow_dispatch' || inputs.mode != 'wait-mail'"
    assert parity['if'] == ordinary_ci, 'H5 PR event selection changed'
    matrix = parity["strategy"]["matrix"]
    assert matrix["os"] == SYSTEMS and "exclude" not in matrix
    assert set(SUITES) <= set(matrix["suite"]), "missing H5 suite"
    step = next(s for s in parity["steps"] if s.get("name") == "Ported rows and daemon controls")
    assert step["if"] == "startsWith(matrix.suite, 'h5-')"
    assert not step.get("continue-on-error")
    assert ["python", "rust/ci/run_coverage.py", "--suite", "${{", "matrix.suite", "}}"] \
        == commands(step["run"])[0][:6]
    assert "--out-dir" in commands(step["run"])[0]
    exit_propagation(step, "python rust/ci/run_coverage.py --suite ${{ matrix.suite }} "
                     "--out-dir (Join-Path $env:RUNNER_TEMP 'h5') --keep-going")
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
    events = workflow.get('on', workflow.get(True))
    assert events.get('push') == {'branches': ['master']} \
        and events.get('schedule') == [{'cron': '23 3 * * *'}], 'source events dropped or changed'
    assert 'pull_request' in events and 'workflow_dispatch' in events, 'source events dropped or changed'
    assert source['if'] == SOURCE_EVENTS, 'source event selection changed'
    gate = jobs['parity']
    assert gate['needs'] == ['rust', 'candidate', 'parity-checks', 'source-mutants'], 'Parity event gate lost dependency'
    assert gate['if'] == f'always() && ({ordinary_ci})' and not gate.get('continue-on-error'), 'Parity event gate can skip failures'
    assert len(gate['steps']) == 1, 'Parity event gate steps changed'
    required = gate['steps'][0]
    assert required.get('shell') == 'pwsh' and not required.get('if') \
        and not required.get('continue-on-error'), 'Parity event gate can skip failures'
    assert required['env'] == {'GATE_RESULTS': '${{ toJSON(needs) }}',
                              'GATE_EVENT': '${{ github.event_name }}'}, 'Parity event gate inputs changed'
    actual = [line.strip() for line in required['run'].splitlines() if line.strip()]
    expected = [line.strip() for line in PARITY_EVENT_GATE.splitlines() if line.strip()]
    assert actual == expected, 'Parity event gate accepts an unsupported result'
    invocation = next(s for s in source["steps"] if s.get("name") == "CLI source mutants")
    assert not invocation.get("if") and not invocation.get("continue-on-error")
    assert commands(invocation["run"])[0][:6] == ["python", "rust/ci/run_coverage.py", "--source-shard", "${{", "matrix.shard", "}}"]
    exit_propagation(invocation, "python rust/ci/run_coverage.py --source-shard ${{ matrix.shard }} "
                     "--out-dir (Join-Path $env:RUNNER_TEMP 'h5-source')")
    oracle = next(s for s in source['steps'] if s.get('name') == 'Bind the source-control Python oracle')
    assert source['steps'].index(oracle) < source['steps'].index(invocation)
    assert oracle['env'] == {'GITHUB_REF': '${{ github.ref }}'}
    assert commands(oracle['run'])[0][:3] == ['python', '-m', 'evals.rust_port.oracle_selection']
    assert '--github-env' in commands(oracle['run'])[0]

    generic = set(assigned_list("rust/daemon/harness/run.py", "MUTANTS"))
    assert generic == set(DAEMON_CONTROLS) | set(EXCEPTIONS["daemon_mutants"]), "daemon control dropped"
    executed = {action["mutant"] for action in SUITES["h5-daemon-mutants"]}
    assert set(DAEMON_CONTROLS) == executed, ("daemon source mutant dropped", set(DAEMON_CONTROLS) - executed)
    assert len(SUITES['h5-daemon-mutants']) == len(DAEMON_CONTROLS), 'executable daemon control duplicated'
    assert all(action == dict(kind='daemon', mode='mutants', mutant=action['mutant'],
                              scenarios=DAEMON_CONTROLS[action['mutant']])
               for action in SUITES['h5-daemon-mutants']), 'executable daemon control lost mode or witnesses'
    for row, path in (("store", "graph_store.py"), ("read", "graph_read_profile.py")):
        assert any(action == {"kind": "graph", "mode": "mutants", "row": row}
                   for action in SUITES["h5-graph-mutants"])
        assert assigned_list("rust/daemon/harness/" + path, "MUTANTS") == GRAPH_CONTROLS[row]
    assert set(W1A) == {action["scenario"] for action in SUITES["h5-w1a"] if action["mode"] == "live"} \
        | set(EXCEPTIONS["w1a"]), "W1-A scenario dropped"
    supported = set(W1A) - set(EXCEPTIONS['w1a'])
    assert set(EXCEPTIONS['w1a_platform']) == {'linux', 'windows'}, 'W1-A platform exception roster changed'
    assert all(set(names) <= supported for names in EXCEPTIONS['w1a_platform'].values()), 'unknown W1-A platform exception'
    assert set(EXCEPTIONS['w1a_golden']) <= supported, 'unknown W1-A golden exception'
    expected_w1a = [dict(kind='daemon', scenario=name, mode=mode, record=mode == 'live')
                    for name in W1A if name in supported for mode in ('live', 'golden')]
    assert SUITES['h5-w1a'] == expected_w1a, 'W1-A recording/replay selection dropped or changed'
    scoped = EXCEPTIONS['w1a_selections']
    exception_id = 'windows-map-order-unsettled-bank-init'
    assert set(scoped) == {exception_id}, 'map-order exception scope changed'
    entry = scoped[exception_id]
    assert set(entry) == {'platform', 'mode', 'scenario', 'reason'} \
        and (entry['platform'], entry['mode'], entry['scenario']) == ('windows', 'both', 'map-order'), \
        'map-order exception scope changed'
    from run_coverage import suite_actions
    for platform in ('linux', 'windows'):
        actions, omitted_scenarios, omitted_selections = suite_actions('h5-w1a', platform)
        protected = [('live', 'map-order-reversed'), ('golden', 'map-order-reversed')]
        if platform == 'linux':
            protected.extend([('live', 'map-order'), ('golden', 'map-order')])
        actual_protected = Counter((a['mode'], a['scenario']) for a in actions
                                   if a['scenario'] in ('map-order', 'map-order-reversed'))
        assert actual_protected == Counter(protected), 'protected map-order selections dropped or changed'
        expected = [a for a in expected_w1a if a['scenario'] not in EXCEPTIONS['w1a_platform'][platform]
                    and not (platform == 'windows' and a['scenario'] == 'map-order')]
        assert actions == expected, 'map-order exception scope changed in executable selection'
        assert omitted_selections == (scoped if platform == 'windows' else {}), 'map-order exception scope changed in receipt'
        assert omitted_scenarios == EXCEPTIONS['w1a_platform'][platform]
    assert SUITES['h5-background'] == [dict(kind='daemon', scenario=name, mode='live')
                                     for name in BACKGROUND], 'accepted daemon scenario lost live background execution'
    existing = set()
    for step in parity['steps']:
        for words in commands(step.get('run', '')):
            if words[:2] == ['python', 'rust/daemon/harness/run.py'] and words[2] in ('live', 'golden') and '--only' in words:
                existing.update(w for w in words[words.index('--only') + 1:] if not w.startswith('--'))
    added = {a['scenario'] for actions in SUITES.values() for a in actions if 'scenario' in a}
    exceptions = set(EXCEPTIONS.get('daemon_scenarios', {}))
    assert exceptions <= daemon_scenarios(), 'unknown daemon scenario exception'
    assert daemon_scenarios() <= existing | added | set(EXCEPTIONS['w1a']) | exceptions, 'accepted daemon scenario missing from CI inventory'
    assert any(s.get("name") == "ported-row-and-mutant-ci-inventory" for s in jobs["rust"]["steps"])


if __name__ == "__main__":
    import yaml
    check(yaml.safe_load((ROOT / ".github/workflows/rust.yml").read_text(encoding="utf-8")))
    print("ported-row-and-mutant-ci-inventory: all named selections accounted for")
